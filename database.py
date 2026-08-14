import os
import json
import time
import secrets
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken

from config import ENCRYPTION_KEY


_local = threading.local()

try:
    cipher = Fernet(ENCRYPTION_KEY.strip().encode())
except Exception:
    raise RuntimeError("ENCRYPTION_KEY is not a valid Fernet key.")


# ============================================================
# Default settings
# ============================================================

DEFAULT_SETTINGS = {
    "STARTUP_DELAY": "10",
    "ACCOUNT_START_INTERVAL": "3",
    "STOP_COOLDOWN_SECONDS": "5",

    # Pishi is interval-based
    "PISHI_INTERVAL_SECONDS": "1800",

    # Dynamic Meow parse timeout
    "DYNAMIC_WAIT_TIMEOUT_SECONDS": "0",

    # Fishing status checks
    "FISHING_STATUS_CHECK_DELAY": "300",
    "FISHING_TIME_CHECK_INTERVAL": "900",

    # Button click retry
    "BUTTON_CLICK_MAX_RETRIES": "10",
    "BUTTON_CLICK_RETRY_DELAY": "1.0",

    # Click delays
    "FISHING_CLICK_DELAY": "2.0",
    "PISHI_CLICK_DELAY": "1.0",
    "MEOW_CLICK_DELAY": "1.0",

    # ms = 4:30 means 4 minutes 30 seconds
    # hm = 4:30 means 4 hours 30 minutes
    "TWO_PART_TIME_MODE": "ms",

    # Profile fetch (میوهام) reply timeout
    "PROFILE_FETCH_TIMEOUT": "20",

    # Concurrent account jobs
    "USER_JOB_CONCURRENCY": "3",

    # Transfer confirmation
    "TRANSFER_CONFIRM_TIMEOUT": "30",
    "TRANSFER_CONFIRM_EDIT_TIMEOUT": "20",
    "TRANSFER_CONFIRM_MAX_RETRIES": "3",

    # Rescue cat
    "RESCUE_MAX_CLICKS": "15",
    "RESCUE_FIRST_CLICK_DELAY": "0.1",
    "RESCUE_FAST_CLICK_MIN_DELAY": "0.10",
    "RESCUE_FAST_CLICK_MAX_DELAY": "0.25",
    "RESCUE_NORMAL_CLICK_DELAY": "1.0",

    # Global automation toggles
    "GLOBAL_AUTOMATION_ENABLED": "1",
    "GLOBAL_MEOW_ENABLED": "1",
    "GLOBAL_PISHI_ENABLED": "1",
    "GLOBAL_FISHING_ENABLED": "1",
    "GLOBAL_RESCUE_ENABLED": "1",
    "GLOBAL_TRANSFER_ENABLED": "1",

    # Maintenance
    "MAINTENANCE_MODE": "0",

    # Command templates
    "MEOW_COMMAND": "میو",
    "PISHI_COMMAND": "پیشی",
    "FISHING_COMMAND": "ماهی",
    "PROFILE_COMMAND": "میوهام",
    "TRANSFER_COMMAND_TEMPLATE": "انتقال میویی {amount} {target}",

    # Heist
    "HEIST_HEARTBEAT_LOG": "0",
}


# ============================================================
# Connection helpers
# ============================================================

def _db_url():
    url = os.getenv("DATABASE_URL", "").strip()

    if not url:
        raise RuntimeError("DATABASE_URL environment variable is missing.")

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    return url


def get_conn():
    if not hasattr(_local, "conn") or _local.conn is None or _local.conn.closed:
        _local.conn = psycopg2.connect(_db_url(), connect_timeout=10)
    else:
        try:
            cur = _local.conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        except psycopg2.OperationalError:
            _local.conn = psycopg2.connect(_db_url(), connect_timeout=10)

    return _local.conn


@contextmanager
def get_db_cursor(commit: bool = False, dict_cursor: bool = False):
    conn = get_conn()

    if dict_cursor:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = conn.cursor()

    try:
        yield cur

        if commit:
            conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        try:
            cur.close()
        except Exception:
            pass


# ============================================================
# Encryption
# ============================================================

def encrypt_session(session_string: str) -> str:
    if not session_string:
        return ""

    return cipher.encrypt(session_string.encode()).decode()


def decrypt_session(encrypted_string: str) -> str:
    if not encrypted_string:
        return ""

    try:
        return cipher.decrypt(encrypted_string.encode()).decode()
    except InvalidToken:
        return encrypted_string


# ============================================================
# Init DB
# ============================================================

def init_db():
    with get_db_cursor(commit=True) as cur:
        # ----------------------------------------------------------
        # Tables
        # ----------------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS web_users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                is_disabled INTEGER DEFAULT 0,
                backup_group_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                used INTEGER DEFAULT 0,
                used_by_user_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tg_accounts (
                phone TEXT PRIMARY KEY,
                uid TEXT DEFAULT '',
                owner_id INTEGER NOT NULL,
                session_string TEXT NOT NULL,
                selected_groups TEXT DEFAULT '[]',
                meow_enabled INTEGER DEFAULT 0,
                pishi_enabled INTEGER DEFAULT 0,
                fishing_enabled INTEGER DEFAULT 0,
                rescue_enabled INTEGER DEFAULT 0,
                rescue_groups TEXT DEFAULT '[]',
                is_active INTEGER DEFAULT 0,
                cached_groups TEXT DEFAULT '[]',
                meow_next_run BIGINT DEFAULT 0,
                pishi_next_run BIGINT DEFAULT 0,
                fishing_next_run BIGINT DEFAULT 0,
                fishing_status_check_at BIGINT DEFAULT 0,
                fishing_periodic_check_at BIGINT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                status TEXT DEFAULT 'running',
                total_accounts INTEGER DEFAULT 0,
                processed_accounts INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS account_logs (
                id SERIAL PRIMARY KEY,
                account_uid TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                feature TEXT DEFAULT '',
                action TEXT DEFAULT '',
                status TEXT DEFAULT '',
                message TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS heist_config (
                user_id INTEGER PRIMARY KEY,
                chat_id BIGINT DEFAULT 0,
                use_backup_group INTEGER DEFAULT 0,
                selected_level INTEGER DEFAULT 1,
                auto_enabled INTEGER DEFAULT 0,
                auto_level_mode TEXT DEFAULT 'best_available',
                steal_count INTEGER DEFAULT 0,
                move_count INTEGER DEFAULT 0,
                listen_timeout INTEGER DEFAULT 600,
                phase_timeout INTEGER DEFAULT 300,
                heartbeat_log INTEGER DEFAULT 0
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS heist_accounts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                position INTEGER NOT NULL,
                UNIQUE(user_id, position)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS heist_cooldowns (
                user_id INTEGER NOT NULL,
                level INTEGER NOT NULL,
                cooldown_until BIGINT DEFAULT 0,
                PRIMARY KEY (user_id, level)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS heist_state (
                user_id INTEGER PRIMARY KEY,
                state TEXT DEFAULT 'idle',
                message_id BIGINT DEFAULT 0,
                chat_id BIGINT DEFAULT 0,
                level INTEGER DEFAULT 0,
                steal_clicks_done INTEGER DEFAULT 0,
                move_clicks_done INTEGER DEFAULT 0,
                started_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT DEFAULT ''
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS heist_logs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                level INTEGER,
                result TEXT,
                duration_seconds INTEGER DEFAULT 0,
                accounts_used TEXT DEFAULT '[]',
                started_at TIMESTAMP,
                finished_at TIMESTAMP
            )
        """)

        # ----------------------------------------------------------
        # Migrations for older databases
        # ----------------------------------------------------------

        cur.execute("""
            DO $$
            BEGIN
                -- web_users migrations
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='web_users' AND column_name='is_disabled'
                ) THEN
                    ALTER TABLE web_users ADD COLUMN is_disabled INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='web_users' AND column_name='backup_group_id'
                ) THEN
                    ALTER TABLE web_users ADD COLUMN backup_group_id TEXT DEFAULT '';
                END IF;

                -- tg_accounts migrations
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='uid'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN uid TEXT DEFAULT '';
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='pishi_enabled'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN pishi_enabled INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='fishing_enabled'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN fishing_enabled INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='rescue_enabled'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN rescue_enabled INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='rescue_groups'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN rescue_groups TEXT DEFAULT '[]';
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='account_name'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN account_name TEXT DEFAULT '';
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='in_backup_group'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN in_backup_group INTEGER DEFAULT -1;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='meow_next_run'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN meow_next_run BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='pishi_next_run'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN pishi_next_run BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='fishing_next_run'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN fishing_next_run BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='fishing_status_check_at'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN fishing_status_check_at BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='fishing_periodic_check_at'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN fishing_periodic_check_at BIGINT DEFAULT 0;
                END IF;

                -- Profile fields
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='balance'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN balance BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='balance_rank'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN balance_rank BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='meow_count'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN meow_count BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='meow_rank'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN meow_rank BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='street_cats'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN street_cats BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='street_cats_rank'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN street_cats_rank BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='level'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN level INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='level_progress'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN level_progress TEXT DEFAULT '';
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='profile_updated_at'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN profile_updated_at BIGINT DEFAULT 0;
                END IF;

                -- Pishi info fields
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='pishi_rank_number'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN pishi_rank_number INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='pishi_level_current'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN pishi_level_current INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='pishi_level_max'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN pishi_level_max INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='pishi_mps'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN pishi_mps BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='pishi_capacity'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN pishi_capacity BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='pishi_upgrade_cost'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN pishi_upgrade_cost BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='pishi_info_updated_at'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN pishi_info_updated_at BIGINT DEFAULT 0;
                END IF;

                -- Session / reliability fields
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='session_status'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN session_status TEXT DEFAULT 'unknown';
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='last_error'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN last_error TEXT DEFAULT '';
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='flood_wait_until'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN flood_wait_until BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='jail_until'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN jail_until BIGINT DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts' AND column_name='jail_reason'
                ) THEN
                    ALTER TABLE tg_accounts ADD COLUMN jail_reason TEXT DEFAULT '';
                END IF;
            END $$;
        """)

        # ----------------------------------------------------------
        # Convert old REAL / DOUBLE PRECISION timer columns to BIGINT
        # ----------------------------------------------------------

        cur.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts'
                      AND column_name='meow_next_run'
                      AND data_type <> 'bigint'
                ) THEN
                    ALTER TABLE tg_accounts
                    ALTER COLUMN meow_next_run
                    TYPE BIGINT
                    USING ROUND(COALESCE(meow_next_run, 0))::BIGINT;

                    ALTER TABLE tg_accounts
                    ALTER COLUMN meow_next_run
                    SET DEFAULT 0;
                END IF;

                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts'
                      AND column_name='pishi_next_run'
                      AND data_type <> 'bigint'
                ) THEN
                    ALTER TABLE tg_accounts
                    ALTER COLUMN pishi_next_run
                    TYPE BIGINT
                    USING ROUND(COALESCE(pishi_next_run, 0))::BIGINT;

                    ALTER TABLE tg_accounts
                    ALTER COLUMN pishi_next_run
                    SET DEFAULT 0;
                END IF;

                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts'
                      AND column_name='fishing_next_run'
                      AND data_type <> 'bigint'
                ) THEN
                    ALTER TABLE tg_accounts
                    ALTER COLUMN fishing_next_run
                    TYPE BIGINT
                    USING ROUND(COALESCE(fishing_next_run, 0))::BIGINT;

                    ALTER TABLE tg_accounts
                    ALTER COLUMN fishing_next_run
                    SET DEFAULT 0;
                END IF;

                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts'
                      AND column_name='fishing_status_check_at'
                      AND data_type <> 'bigint'
                ) THEN
                    ALTER TABLE tg_accounts
                    ALTER COLUMN fishing_status_check_at
                    TYPE BIGINT
                    USING ROUND(COALESCE(fishing_status_check_at, 0))::BIGINT;

                    ALTER TABLE tg_accounts
                    ALTER COLUMN fishing_status_check_at
                    SET DEFAULT 0;
                END IF;

                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='tg_accounts'
                      AND column_name='fishing_periodic_check_at'
                      AND data_type <> 'bigint'
                ) THEN
                    ALTER TABLE tg_accounts
                    ALTER COLUMN fishing_periodic_check_at
                    TYPE BIGINT
                    USING ROUND(COALESCE(fishing_periodic_check_at, 0))::BIGINT;

                    ALTER TABLE tg_accounts
                    ALTER COLUMN fishing_periodic_check_at
                    SET DEFAULT 0;
                END IF;
            END $$;
        """)

        # ----------------------------------------------------------
        # Insert default settings
        # ----------------------------------------------------------

        for key, value in DEFAULT_SETTINGS.items():
            cur.execute(
                """
                INSERT INTO settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (key, value)
            )

    # ----------------------------------------------------------
    # Ensure UIDs exist for old accounts
    # ----------------------------------------------------------

    ensure_account_uids()

    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_accounts_uid
            ON tg_accounts(uid)
            """
        )

    # ----------------------------------------------------------
    # Create admin user if env vars are set
    # ----------------------------------------------------------

    admin_user = os.getenv("ADMIN_USERNAME")
    admin_pass = os.getenv("ADMIN_PASSWORD")

    if admin_user and admin_pass:
        if not get_web_user(admin_user):
            create_web_user(admin_user, admin_pass, is_admin=True)
            print(f"✅ Created admin user: {admin_user}")


# ============================================================
# Settings
# ============================================================

def get_setting(key, default=None):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()

    if row:
        return row["value"]

    return DEFAULT_SETTINGS.get(key, default)


def get_all_settings():
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT key, value FROM settings")
        rows = cur.fetchall()

    data = DEFAULT_SETTINGS.copy()

    for row in rows:
        data[row["key"]] = row["value"]

    return data


def set_setting(key, value):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value
            """,
            (key, str(value))
        )


def get_setting_int(key, default=0):
    value = get_setting(key, str(default))

    try:
        return int(float(value))
    except:
        return default


def get_setting_float(key, default=0.0):
    value = get_setting(key, str(default))

    try:
        return float(value)
    except:
        return default


# ============================================================
# Web users
# ============================================================

def create_web_user(username, password, is_admin=False):
    pw_hash = generate_password_hash(password)

    with get_db_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO web_users (username, password_hash, is_admin) VALUES (%s, %s, %s)",
            (username, pw_hash, 1 if is_admin else 0)
        )


def get_web_user(username):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM web_users WHERE username = %s", (username,))
        row = cur.fetchone()

    if not row:
        return None

    result = dict(row)
    result["is_disabled"] = bool(result.get("is_disabled"))
    result["is_admin"] = bool(result.get("is_admin"))

    return result


def get_web_user_by_id(user_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM web_users WHERE id = %s", (user_id,))
        row = cur.fetchone()

    if not row:
        return None

    result = dict(row)
    result["is_disabled"] = bool(result.get("is_disabled"))
    result["is_admin"] = bool(result.get("is_admin"))

    return result


def verify_web_user(username, password):
    user = get_web_user(username)

    if user and check_password_hash(user["password_hash"], password):
        return user

    return None


def set_backup_group_id(user_id, group_id):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE web_users SET backup_group_id = %s WHERE id = %s",
            (str(group_id or ""), user_id)
        )


def set_user_disabled(user_id, disabled):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE web_users SET is_disabled = %s WHERE id = %s",
            (1 if disabled else 0, user_id)
        )


def get_all_web_users_with_counts():
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT
                u.*,
                (
                    SELECT COUNT(*)
                    FROM tg_accounts a
                    WHERE a.owner_id = u.id
                ) AS accounts_count
            FROM web_users u
            ORDER BY u.created_at DESC
            """
        )

        rows = [dict(row) for row in cur.fetchall()]

        for row in rows:
            row["is_disabled"] = bool(row.get("is_disabled"))
            row["is_admin"] = bool(row.get("is_admin"))

        return rows


# ============================================================
# Invites
# ============================================================

def create_invite(code):
    try:
        with get_db_cursor(commit=True) as cur:
            cur.execute("INSERT INTO invites (code) VALUES (%s)", (code,))
        return True
    except psycopg2.IntegrityError:
        return False


def get_all_invites():
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM invites ORDER BY created_at DESC")
        rows = [dict(row) for row in cur.fetchall()]

    return rows


def get_valid_invite(code):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM invites WHERE code = %s AND used = 0", (code,))
        row = cur.fetchone()

    return dict(row) if row else None


def use_invite(code, user_id):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE invites SET used = 1, used_by_user_id = %s WHERE code = %s",
            (user_id, code)
        )


# ============================================================
# UID helpers
# ============================================================

def generate_account_uid():
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def generate_unique_account_uid():
    for _ in range(30):
        uid = generate_account_uid()

        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute(
                "SELECT 1 FROM tg_accounts WHERE uid = %s",
                (uid,)
            )

            if not cur.fetchone():
                return uid

    raise RuntimeError("Could not generate a unique account UID")


def ensure_account_uids():
    with get_db_cursor(commit=True, dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT uid
            FROM tg_accounts
            WHERE uid IS NOT NULL
              AND uid <> ''
            """
        )

        existing_uids = {row["uid"] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT phone
            FROM tg_accounts
            WHERE uid IS NULL
               OR uid = ''
            """
        )

        missing = cur.fetchall()

        for row in missing:
            for _ in range(30):
                uid = generate_account_uid()

                if uid not in existing_uids:
                    break
            else:
                raise RuntimeError("Could not generate a unique account UID")

            existing_uids.add(uid)

            cur.execute(
                "UPDATE tg_accounts SET uid = %s WHERE phone = %s",
                (uid, row["phone"])
            )


# ============================================================
# Phone masking
# ============================================================

def mask_phone(phone):
    s = str(phone or "").strip()

    if len(s) <= 4:
        return "****"

    return s[:3] + "***" + s[-4:]


# ============================================================
# Account logs
# ============================================================

def add_account_log(phone, feature, action, status, message, account_uid=None):
    try:
        uid = account_uid or ""

        if not uid:
            account = get_tg_account(phone)
            uid = (account or {}).get("uid", "")

        with get_db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO account_logs (
                    account_uid,
                    phone,
                    feature,
                    action,
                    status,
                    message
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    uid,
                    str(phone or ""),
                    str(feature or ""),
                    str(action or ""),
                    str(status or ""),
                    str(message or "")
                )
            )

    except Exception as e:
        print(f"❌ add_account_log error: {e}")


def get_account_logs(account_uid, limit=200):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT *
            FROM account_logs
            WHERE account_uid = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (str(account_uid or ""), int(limit))
        )

        return [dict(row) for row in cur.fetchall()]


# ============================================================
# Session status / FloodWait
# ============================================================

def update_session_status(phone, status, error=""):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET
                session_status = %s,
                last_error = %s
            WHERE phone = %s
            """,
            (str(status or "unknown"), str(error or ""), str(phone))
        )


def set_flood_wait(phone, seconds):
    seconds = max(0, int(seconds))
    until = int(time.time()) + seconds

    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET flood_wait_until = %s
            WHERE phone = %s
            """,
            (until, str(phone))
        )


def clear_flood_wait(phone):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET flood_wait_until = 0
            WHERE phone = %s
            """,
            (str(phone),)
        )


# ============================================================
# Telegram accounts — JSON / bool helpers
# ============================================================

def _json_dumps(value):
    return json.dumps(value or [], ensure_ascii=False)


def _json_loads(value):
    try:
        return json.loads(value or "[]")
    except:
        return []


def _bool(value):
    return 1 if value else 0


def _int_or_none(value):
    if value is None:
        return None

    try:
        return int(float(value))
    except:
        return None


# ============================================================
# Telegram accounts — row to dict
# ============================================================

def _row_to_account(row):
    if not row:
        return None

    acc = dict(row)

    acc["session_string"] = decrypt_session(acc.get("session_string", ""))
    acc["selected_groups"] = _json_loads(acc.get("selected_groups"))
    acc["cached_groups"] = _json_loads(acc.get("cached_groups"))
    acc["rescue_groups"] = _json_loads(acc.get("rescue_groups"))

    acc["meow_enabled"] = bool(acc.get("meow_enabled"))
    acc["pishi_enabled"] = bool(acc.get("pishi_enabled"))
    acc["fishing_enabled"] = bool(acc.get("fishing_enabled"))
    acc["rescue_enabled"] = bool(acc.get("rescue_enabled"))
    acc["is_active"] = bool(acc.get("is_active"))

    acc["meow_next_run"] = int(float(acc.get("meow_next_run") or 0))
    acc["pishi_next_run"] = int(float(acc.get("pishi_next_run") or 0))
    acc["fishing_next_run"] = int(float(acc.get("fishing_next_run") or 0))

    acc["fishing_status_check_at"] = int(float(acc.get("fishing_status_check_at") or 0))
    acc["fishing_periodic_check_at"] = int(float(acc.get("fishing_periodic_check_at") or 0))

    # UID / name / backup
    acc["uid"] = acc.get("uid") or ""
    acc["account_name"] = acc.get("account_name") or ""

    try:
        acc["in_backup_group"] = int(acc.get("in_backup_group"))
    except Exception:
        acc["in_backup_group"] = -1

    # Profile fields
    acc["balance"] = int(float(acc.get("balance") or 0))
    acc["balance_rank"] = int(float(acc.get("balance_rank") or 0))
    acc["meow_count"] = int(float(acc.get("meow_count") or 0))
    acc["meow_rank"] = int(float(acc.get("meow_rank") or 0))
    acc["street_cats"] = int(float(acc.get("street_cats") or 0))
    acc["street_cats_rank"] = int(float(acc.get("street_cats_rank") or 0))
    acc["level"] = int(float(acc.get("level") or 0))
    acc["level_progress"] = acc.get("level_progress") or ""
    acc["profile_updated_at"] = int(float(acc.get("profile_updated_at") or 0))

    # Pishi info fields
    acc["pishi_rank_number"] = int(float(acc.get("pishi_rank_number") or 0))
    acc["pishi_level_current"] = int(float(acc.get("pishi_level_current") or 0))
    acc["pishi_level_max"] = int(float(acc.get("pishi_level_max") or 0))
    acc["pishi_mps"] = int(float(acc.get("pishi_mps") or 0))
    acc["pishi_capacity"] = int(float(acc.get("pishi_capacity") or 0))
    acc["pishi_upgrade_cost"] = int(float(acc.get("pishi_upgrade_cost") or 0))
    acc["pishi_info_updated_at"] = int(float(acc.get("pishi_info_updated_at") or 0))

    # Session / reliability fields
    acc["session_status"] = acc.get("session_status") or "unknown"
    acc["last_error"] = acc.get("last_error") or ""
    acc["flood_wait_until"] = int(float(acc.get("flood_wait_until") or 0))

    # Compatibility alias
    acc["fish_enabled"] = acc["pishi_enabled"]

    acc["jail_until"] = int(float(acc.get("jail_until") or 0))
    acc["jail_reason"] = acc.get("jail_reason") or ""

    return acc


# ============================================================
# Telegram accounts — save / get / delete
# ============================================================

def save_tg_account(
    phone,
    owner_id,
    session_string,
    selected_groups=None,
    meow_enabled=False,
    pishi_enabled=False,
    fishing_enabled=False,
    rescue_enabled=None,
    rescue_groups=None,
    is_active=False,
    cached_groups=None,
    meow_next_run=None,
    pishi_next_run=None,
    fishing_next_run=None
):
    existing = get_tg_account(phone)

    if existing:
        uid = existing.get("uid") or generate_unique_account_uid()

        if meow_next_run is None:
            meow_next_run = existing.get("meow_next_run", 0)

        if pishi_next_run is None:
            pishi_next_run = existing.get("pishi_next_run", 0)

        if fishing_next_run is None:
            fishing_next_run = existing.get("fishing_next_run", 0)

        if rescue_enabled is None:
            rescue_enabled = existing.get("rescue_enabled", False)

        if rescue_groups is None:
            rescue_groups = existing.get("rescue_groups", [])
    else:
        uid = generate_unique_account_uid()

        if meow_next_run is None:
            meow_next_run = 0

        if pishi_next_run is None:
            pishi_next_run = 0

        if fishing_next_run is None:
            fishing_next_run = 0

        if rescue_enabled is None:
            rescue_enabled = False

        if rescue_groups is None:
            rescue_groups = []

    encrypted_session = encrypt_session(session_string)

    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO tg_accounts (
                phone,
                uid,
                owner_id,
                session_string,
                selected_groups,
                meow_enabled,
                pishi_enabled,
                fishing_enabled,
                rescue_enabled,
                rescue_groups,
                is_active,
                cached_groups,
                meow_next_run,
                pishi_next_run,
                fishing_next_run
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (phone) DO UPDATE SET
                uid = EXCLUDED.uid,
                owner_id = EXCLUDED.owner_id,
                session_string = EXCLUDED.session_string,
                selected_groups = EXCLUDED.selected_groups,
                meow_enabled = EXCLUDED.meow_enabled,
                pishi_enabled = EXCLUDED.pishi_enabled,
                fishing_enabled = EXCLUDED.fishing_enabled,
                rescue_enabled = EXCLUDED.rescue_enabled,
                rescue_groups = EXCLUDED.rescue_groups,
                is_active = EXCLUDED.is_active,
                cached_groups = EXCLUDED.cached_groups,
                meow_next_run = EXCLUDED.meow_next_run,
                pishi_next_run = EXCLUDED.pishi_next_run,
                fishing_next_run = EXCLUDED.fishing_next_run
            """,
            (
                str(phone),
                uid,
                owner_id,
                encrypted_session,
                _json_dumps(selected_groups),
                _bool(meow_enabled),
                _bool(pishi_enabled),
                _bool(fishing_enabled),
                _bool(rescue_enabled),
                _json_dumps(rescue_groups),
                _bool(is_active),
                _json_dumps(cached_groups),
                int(float(meow_next_run or 0)),
                int(float(pishi_next_run or 0)),
                int(float(fishing_next_run or 0))
            )
        )


def get_tg_account(phone):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM tg_accounts WHERE phone = %s", (str(phone),))
        row = cur.fetchone()

    return _row_to_account(row)


def get_tg_account_by_uid(uid):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT *
            FROM tg_accounts
            WHERE uid = %s
              AND uid <> ''
            """,
            (str(uid or ""),)
        )
        row = cur.fetchone()

    return _row_to_account(row)


def get_tg_accounts_for_user(owner_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            "SELECT * FROM tg_accounts WHERE owner_id = %s ORDER BY created_at DESC",
            (owner_id,)
        )
        rows = [_row_to_account(row) for row in cur.fetchall()]

    return rows


def get_all_tg_accounts():
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM tg_accounts")
        rows = [_row_to_account(row) for row in cur.fetchall()]

    return rows


def delete_tg_account(phone, owner_id):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM tg_accounts WHERE phone = %s AND owner_id = %s",
            (str(phone), owner_id)
        )


def update_account_next_run(
    phone,
    meow_next_run=None,
    pishi_next_run=None,
    fishing_next_run=None
):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET
                meow_next_run = COALESCE(%s, meow_next_run),
                pishi_next_run = COALESCE(%s, pishi_next_run),
                fishing_next_run = COALESCE(%s, fishing_next_run)
            WHERE phone = %s
            """,
            (
                _int_or_none(meow_next_run),
                _int_or_none(pishi_next_run),
                _int_or_none(fishing_next_run),
                str(phone)
            )
        )


def update_account_meta(phone, account_name=None, in_backup_group=None):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET
                account_name = COALESCE(%s, account_name),
                in_backup_group = COALESCE(%s, in_backup_group)
            WHERE phone = %s
            """,
            (account_name, in_backup_group, str(phone))
        )


def update_account_profile(
    phone,
    account_name=None,
    balance=None,
    balance_rank=None,
    meow_count=None,
    meow_rank=None,
    street_cats=None,
    street_cats_rank=None,
    level=None,
    level_progress=None
):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET
                account_name = COALESCE(%s, account_name),
                balance = COALESCE(%s, balance),
                balance_rank = COALESCE(%s, balance_rank),
                meow_count = COALESCE(%s, meow_count),
                meow_rank = COALESCE(%s, meow_rank),
                street_cats = COALESCE(%s, street_cats),
                street_cats_rank = COALESCE(%s, street_cats_rank),
                level = COALESCE(%s, level),
                level_progress = COALESCE(%s, level_progress),
                profile_updated_at = %s
            WHERE phone = %s
            """,
            (
                account_name,
                balance,
                balance_rank,
                meow_count,
                meow_rank,
                street_cats,
                street_cats_rank,
                level,
                level_progress,
                int(time.time()),
                str(phone)
            )
        )


def update_pishi_info(phone, data):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET
                pishi_rank_number = COALESCE(%s, pishi_rank_number),
                pishi_level_current = COALESCE(%s, pishi_level_current),
                pishi_level_max = COALESCE(%s, pishi_level_max),
                pishi_mps = COALESCE(%s, pishi_mps),
                pishi_capacity = COALESCE(%s, pishi_capacity),
                pishi_upgrade_cost = COALESCE(%s, pishi_upgrade_cost),
                pishi_info_updated_at = %s
            WHERE phone = %s
            """,
            (
                data.get("pishi_rank_number"),
                data.get("pishi_level_current"),
                data.get("pishi_level_max"),
                data.get("pishi_mps"),
                data.get("pishi_capacity"),
                data.get("pishi_upgrade_cost"),
                int(time.time()),
                str(phone)
            )
        )


# ============================================================
# Atomic trigger claims
# ============================================================

def claim_dynamic_feature(
    phone,
    feature,
    mode,
    waiting_timestamp,
    now,
    timeout=0
):
    columns = {
        "meow": "meow_next_run",
        "fishing": "fishing_next_run",
    }

    col = columns.get(feature)

    if not col:
        return False

    try:
        waiting_timestamp = int(float(waiting_timestamp))
        now = int(float(now))

        with get_db_cursor(commit=True) as cur:
            if mode == "send_initial":
                cur.execute(
                    f"""
                    UPDATE tg_accounts
                    SET {col} = %s
                    WHERE phone = %s
                      AND ({col} IS NULL OR {col} = 0)
                    """,
                    (waiting_timestamp, str(phone))
                )

            elif mode == "send_due":
                cur.execute(
                    f"""
                    UPDATE tg_accounts
                    SET {col} = %s
                    WHERE phone = %s
                      AND {col} > 0
                      AND {col} <= %s
                    """,
                    (waiting_timestamp, str(phone), now)
                )

            elif mode == "retry_after_parse_timeout":
                timeout = max(0, int(float(timeout)))
                cutoff = now - timeout

                cur.execute(
                    f"""
                    UPDATE tg_accounts
                    SET {col} = %s
                    WHERE phone = %s
                      AND {col} < 0
                      AND (-{col}) <= %s
                    """,
                    (waiting_timestamp, str(phone), cutoff)
                )

            else:
                return False

            claimed = cur.rowcount > 0

        return claimed

    except Exception as e:
        print(f"❌ claim_dynamic_feature error [{phone}] [{feature}] [{mode}]: {e}")
        return False


def claim_interval_feature(phone, feature, scheduled_timestamp, now):
    columns = {
        "pishi": "pishi_next_run",
    }

    col = columns.get(feature)

    if not col:
        return False

    try:
        scheduled_timestamp = int(float(scheduled_timestamp))
        now = int(float(now))

        with get_db_cursor(commit=True) as cur:
            cur.execute(
                f"""
                UPDATE tg_accounts
                SET {col} = %s
                WHERE phone = %s
                  AND ({col} IS NULL OR {col} <= %s)
                """,
                (scheduled_timestamp, str(phone), now)
            )
            claimed = cur.rowcount > 0

        return claimed

    except Exception as e:
        print(f"❌ claim_interval_feature error [{phone}] [{feature}]: {e}")
        return False


# ============================================================
# Fishing status / periodic check helpers
# ============================================================

def update_fishing_status_check_at(phone, timestamp):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET fishing_status_check_at = %s
            WHERE phone = %s
            """,
            (int(float(timestamp)), str(phone))
        )


def claim_fishing_status_check(phone, now):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET fishing_status_check_at = 0
            WHERE phone = %s
              AND fishing_status_check_at > 0
              AND fishing_status_check_at <= %s
            """,
            (str(phone), int(float(now)))
        )
        claimed = cur.rowcount > 0

    return claimed


def claim_fishing_periodic_check(phone, next_check_at, now):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET fishing_periodic_check_at = %s
            WHERE phone = %s
              AND (
                  fishing_periodic_check_at IS NULL
                  OR fishing_periodic_check_at <= %s
              )
            """,
            (int(float(next_check_at)), str(phone), int(float(now)))
        )
        claimed = cur.rowcount > 0

    return claimed


def reset_fishing_checks(phone):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET
                fishing_status_check_at = 0,
                fishing_periodic_check_at = 0
            WHERE phone = %s
            """,
            (str(phone),)
        )


# ============================================================
# Command templates
# ============================================================

def get_command_template(key, default):
    try:
        value = get_setting(key, default)
        return value if value else default
    except:
        return default


# ============================================================
# Jobs system
# ============================================================

def create_job(user_id, job_type, total_accounts):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO jobs (user_id, type, total_accounts)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (user_id, str(job_type), int(total_accounts))
        )
        row = cur.fetchone()
        return row[0] if row else None


def update_job_progress(job_id, processed=None, success=None, failed=None):
    if not job_id:
        return

    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE jobs
            SET
                processed_accounts = COALESCE(%s, processed_accounts),
                success_count = COALESCE(%s, success_count),
                failed_count = COALESCE(%s, failed_count)
            WHERE id = %s
            """,
            (
                processed if processed is not None else None,
                success if success is not None else None,
                failed if failed is not None else None,
                job_id
            )
        )


def increment_job_processed(job_id, success=True):
    if not job_id:
        return

    with get_db_cursor(commit=True) as cur:
        if success:
            cur.execute(
                """
                UPDATE jobs
                SET
                    processed_accounts = processed_accounts + 1,
                    success_count = success_count + 1
                WHERE id = %s
                """,
                (job_id,)
            )
        else:
            cur.execute(
                """
                UPDATE jobs
                SET
                    processed_accounts = processed_accounts + 1,
                    failed_count = failed_count + 1
                WHERE id = %s
                """,
                (job_id,)
            )


def finish_job(job_id, status='completed'):
    if not job_id:
        return

    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE jobs
            SET status = %s, finished_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (str(status), job_id)
        )


def get_active_jobs_for_user(user_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT *
            FROM jobs
            WHERE user_id = %s
              AND status = 'running'
            ORDER BY id DESC
            """,
            (user_id,)
        )
        return [dict(row) for row in cur.fetchall()]


def get_recent_jobs_for_user(user_id, limit=10):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT *
            FROM jobs
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (user_id, int(limit))
        )
        return [dict(row) for row in cur.fetchall()]


def get_job_by_id(job_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            "SELECT * FROM jobs WHERE id = %s",
            (job_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ============================================================
# Jail helpers
# ============================================================

def set_account_jail(phone, jail_until, jail_reason=""):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET jail_until = %s, jail_reason = %s
            WHERE phone = %s
            """,
            (int(jail_until), str(jail_reason or ""), str(phone))
        )


def clear_account_jail(phone):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE tg_accounts
            SET jail_until = 0, jail_reason = ''
            WHERE phone = %s
            """,
            (str(phone),)
        )


def is_account_jailed(phone):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            "SELECT jail_until FROM tg_accounts WHERE phone = %s",
            (str(phone),)
        )
        row = cur.fetchone()

    if not row:
        return False

    return int(row.get("jail_until") or 0) > int(time.time())


# ============================================================
# Heist config
# ============================================================

def get_heist_config(user_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            "SELECT * FROM heist_config WHERE user_id = %s",
            (user_id,)
        )
        row = cur.fetchone()

    if row:
        return dict(row)

    return {
        "user_id": user_id,
        "chat_id": 0,
        "use_backup_group": 0,
        "selected_level": 1,
        "auto_enabled": 0,
        "auto_level_mode": "best_available",
        "steal_count": 0,
        "move_count": 0,
        "listen_timeout": 600,
        "phase_timeout": 300,
        "heartbeat_log": 0,
    }


def save_heist_config(user_id, config):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO heist_config (
                user_id, chat_id, use_backup_group, selected_level,
                auto_enabled, auto_level_mode, steal_count, move_count,
                listen_timeout, phase_timeout, heartbeat_log
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                chat_id = EXCLUDED.chat_id,
                use_backup_group = EXCLUDED.use_backup_group,
                selected_level = EXCLUDED.selected_level,
                auto_enabled = EXCLUDED.auto_enabled,
                auto_level_mode = EXCLUDED.auto_level_mode,
                steal_count = EXCLUDED.steal_count,
                move_count = EXCLUDED.move_count,
                listen_timeout = EXCLUDED.listen_timeout,
                phase_timeout = EXCLUDED.phase_timeout,
                heartbeat_log = EXCLUDED.heartbeat_log
            """,
            (
                user_id,
                int(config.get("chat_id", 0)),
                int(config.get("use_backup_group", 0)),
                int(config.get("selected_level", 1)),
                int(config.get("auto_enabled", 0)),
                str(config.get("auto_level_mode", "best_available")),
                int(config.get("steal_count", 0)),
                int(config.get("move_count", 0)),
                int(config.get("listen_timeout", 600)),
                int(config.get("phase_timeout", 300)),
                int(config.get("heartbeat_log", 0)),
            )
        )


# ============================================================
# Heist accounts
# ============================================================

def get_heist_accounts(user_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT * FROM heist_accounts
            WHERE user_id = %s
            ORDER BY position ASC
            """,
            (user_id,)
        )
        return [dict(row) for row in cur.fetchall()]


def save_heist_accounts(user_id, accounts):
    """
    accounts: list of dicts with 'phone' and 'position' keys.
    Replaces all existing assignments for this user.
    """
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM heist_accounts WHERE user_id = %s",
            (user_id,)
        )

        for acc in accounts:
            cur.execute(
                """
                INSERT INTO heist_accounts (user_id, phone, position)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, position) DO UPDATE SET
                    phone = EXCLUDED.phone
                """,
                (user_id, str(acc["phone"]), int(acc["position"]))
            )


def clear_heist_accounts(user_id):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM heist_accounts WHERE user_id = %s",
            (user_id,)
        )


# ============================================================
# Heist cooldowns
# ============================================================

def get_heist_cooldown(user_id, level):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT cooldown_until FROM heist_cooldowns
            WHERE user_id = %s AND level = %s
            """,
            (user_id, int(level))
        )
        row = cur.fetchone()

    if row:
        return int(row.get("cooldown_until") or 0)

    return 0


def set_heist_cooldown(user_id, level, cooldown_until):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO heist_cooldowns (user_id, level, cooldown_until)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, level) DO UPDATE SET
                cooldown_until = EXCLUDED.cooldown_until
            """,
            (user_id, int(level), int(cooldown_until))
        )


def get_all_heist_cooldowns(user_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT level, cooldown_until FROM heist_cooldowns
            WHERE user_id = %s
            ORDER BY level ASC
            """,
            (user_id,)
        )
        return {row["level"]: int(row["cooldown_until"]) for row in cur.fetchall()}


def clear_heist_cooldowns(user_id):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM heist_cooldowns WHERE user_id = %s",
            (user_id,)
        )


# ============================================================
# Heist state
# ============================================================

def get_heist_state(user_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            "SELECT * FROM heist_state WHERE user_id = %s",
            (user_id,)
        )
        row = cur.fetchone()

    if row:
        return dict(row)

    return {
        "user_id": user_id,
        "state": "idle",
        "message_id": 0,
        "chat_id": 0,
        "level": 0,
        "steal_clicks_done": 0,
        "move_clicks_done": 0,
        "started_at": None,
        "updated_at": None,
        "error_message": "",
    }


def set_heist_state(user_id, state, message_id=0, chat_id=0, level=0, error=""):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO heist_state (
                user_id, state, message_id, chat_id, level,
                steal_clicks_done, move_clicks_done,
                started_at, updated_at, error_message
            )
            VALUES (%s, %s, %s, %s, %s, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                state = EXCLUDED.state,
                message_id = EXCLUDED.message_id,
                chat_id = EXCLUDED.chat_id,
                level = EXCLUDED.level,
                steal_clicks_done = 0,
                move_clicks_done = 0,
                started_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP,
                error_message = EXCLUDED.error_message
            """,
            (user_id, str(state), int(message_id), int(chat_id), int(level), str(error))
        )


def update_heist_state(user_id, **kwargs):
    if not kwargs:
        return

    allowed = {
        "state", "message_id", "chat_id", "level",
        "steal_clicks_done", "move_clicks_done",
        "error_message"
    }

    fields = []
    values = []

    for key, value in kwargs.items():
        if key in allowed:
            fields.append(f"{key} = %s")
            values.append(value)

    if not fields:
        return

    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(user_id)

    with get_db_cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE heist_state SET {', '.join(fields)} WHERE user_id = %s",
            values
        )


def reset_heist_state(user_id):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO heist_state (user_id, state)
            VALUES (%s, 'idle')
            ON CONFLICT (user_id) DO UPDATE SET
                state = 'idle',
                message_id = 0,
                chat_id = 0,
                level = 0,
                steal_clicks_done = 0,
                move_clicks_done = 0,
                error_message = '',
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id,)
        )


# ============================================================
# Heist logs
# ============================================================

def add_heist_log(user_id, level, result, duration_seconds, accounts_used):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO heist_logs (
                user_id, level, result, duration_seconds,
                accounts_used, started_at, finished_at
            )
            VALUES (%s, %s, %s, %s, %s,
                    CURRENT_TIMESTAMP - (%s || ' seconds')::INTERVAL,
                    CURRENT_TIMESTAMP)
            """,
            (
                user_id,
                int(level),
                str(result),
                int(duration_seconds),
                json.dumps(accounts_used, ensure_ascii=False),
                int(duration_seconds),
            )
        )


def get_heist_logs(user_id, limit=10):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """
            SELECT * FROM heist_logs
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (user_id, int(limit))
        )
        return [dict(row) for row in cur.fetchall()]