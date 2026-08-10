import os
import json
import time
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
}


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


def init_db():
    with get_db_cursor(commit=True) as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS web_users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
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
                owner_id INTEGER NOT NULL,
                session_string TEXT NOT NULL,
                selected_groups TEXT DEFAULT '[]',
                meow_enabled INTEGER DEFAULT 0,
                pishi_enabled INTEGER DEFAULT 0,
                fishing_enabled INTEGER DEFAULT 0,
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
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Compatibility migrations for older databases
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='web_users' AND column_name='backup_group_id'
                ) THEN
                    ALTER TABLE web_users ADD COLUMN backup_group_id TEXT DEFAULT '';
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
            END $$;
        """)

        # Convert old REAL / DOUBLE PRECISION timer columns to BIGINT
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

        # Insert default settings
        for key, value in DEFAULT_SETTINGS.items():
            cur.execute(
                """
                INSERT INTO settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (key, value)
            )

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

    return dict(row) if row else None


def get_web_user_by_id(user_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM web_users WHERE id = %s", (user_id,))
        row = cur.fetchone()

    return dict(row) if row else None


def set_backup_group_id(user_id, group_id):
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE web_users SET backup_group_id = %s WHERE id = %s",
            (str(group_id or ""), user_id)
        )


def verify_web_user(username, password):
    user = get_web_user(username)

    if user and check_password_hash(user["password_hash"], password):
        return user

    return None


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
# Telegram accounts
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


def _row_to_account(row):
    if not row:
        return None

    acc = dict(row)

    acc["session_string"] = decrypt_session(acc.get("session_string", ""))
    acc["selected_groups"] = _json_loads(acc.get("selected_groups"))
    acc["cached_groups"] = _json_loads(acc.get("cached_groups"))

    acc["meow_enabled"] = bool(acc.get("meow_enabled"))
    acc["pishi_enabled"] = bool(acc.get("pishi_enabled"))
    acc["fishing_enabled"] = bool(acc.get("fishing_enabled"))
    acc["is_active"] = bool(acc.get("is_active"))

    acc["meow_next_run"] = int(float(acc.get("meow_next_run") or 0))
    acc["pishi_next_run"] = int(float(acc.get("pishi_next_run") or 0))
    acc["fishing_next_run"] = int(float(acc.get("fishing_next_run") or 0))

    acc["fishing_status_check_at"] = int(float(acc.get("fishing_status_check_at") or 0))
    acc["fishing_periodic_check_at"] = int(float(acc.get("fishing_periodic_check_at") or 0))

    # New profile / backup fields
    acc["account_name"] = acc.get("account_name") or ""

    try:
        acc["in_backup_group"] = int(acc.get("in_backup_group"))
    except Exception:
        acc["in_backup_group"] = -1

    acc["balance"] = int(float(acc.get("balance") or 0))
    acc["balance_rank"] = int(float(acc.get("balance_rank") or 0))
    acc["meow_count"] = int(float(acc.get("meow_count") or 0))
    acc["meow_rank"] = int(float(acc.get("meow_rank") or 0))
    acc["street_cats"] = int(float(acc.get("street_cats") or 0))
    acc["street_cats_rank"] = int(float(acc.get("street_cats_rank") or 0))
    acc["level"] = int(float(acc.get("level") or 0))
    acc["level_progress"] = acc.get("level_progress") or ""
    acc["profile_updated_at"] = int(float(acc.get("profile_updated_at") or 0))

    # Compatibility alias
    acc["fish_enabled"] = acc["pishi_enabled"]

    return acc


def save_tg_account(
    phone,
    owner_id,
    session_string,
    selected_groups=None,
    meow_enabled=False,
    pishi_enabled=False,
    fishing_enabled=False,
    is_active=False,
    cached_groups=None,
    meow_next_run=None,
    pishi_next_run=None,
    fishing_next_run=None
):
    existing = get_tg_account(phone)

    if existing:
        if meow_next_run is None:
            meow_next_run = existing.get("meow_next_run", 0)

        if pishi_next_run is None:
            pishi_next_run = existing.get("pishi_next_run", 0)

        if fishing_next_run is None:
            fishing_next_run = existing.get("fishing_next_run", 0)
    else:
        if meow_next_run is None:
            meow_next_run = 0

        if pishi_next_run is None:
            pishi_next_run = 0

        if fishing_next_run is None:
            fishing_next_run = 0

    encrypted_session = encrypt_session(session_string)

    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO tg_accounts (
                phone,
                owner_id,
                session_string,
                selected_groups,
                meow_enabled,
                pishi_enabled,
                fishing_enabled,
                is_active,
                cached_groups,
                meow_next_run,
                pishi_next_run,
                fishing_next_run
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (phone) DO UPDATE SET
                owner_id = EXCLUDED.owner_id,
                session_string = EXCLUDED.session_string,
                selected_groups = EXCLUDED.selected_groups,
                meow_enabled = EXCLUDED.meow_enabled,
                pishi_enabled = EXCLUDED.pishi_enabled,
                fishing_enabled = EXCLUDED.fishing_enabled,
                is_active = EXCLUDED.is_active,
                cached_groups = EXCLUDED.cached_groups,
                meow_next_run = EXCLUDED.meow_next_run,
                pishi_next_run = EXCLUDED.pishi_next_run,
                fishing_next_run = EXCLUDED.fishing_next_run
            """,
            (
                str(phone),
                owner_id,
                encrypted_session,
                _json_dumps(selected_groups),
                _bool(meow_enabled),
                _bool(pishi_enabled),
                _bool(fishing_enabled),
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