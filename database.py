import os
import json
import threading

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

    "MEOW_FALLBACK_SECONDS": "300",
    "PISHI_INTERVAL_SECONDS": "1800",
    "FISHING_INTERVAL_SECONDS": "600",

    "FISHING_CLICK_DELAY": "2.0",
    "PISHI_CLICK_DELAY": "1.0",
    "MEOW_CLICK_DELAY": "1.0",

    # ms = 4:30 means 4 minutes 30 seconds
    # hm = 4:30 means 4 hours 30 minutes
    "TWO_PART_TIME_MODE": "ms",
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
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS web_users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
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
            meow_next_run REAL DEFAULT 0,
            pishi_next_run REAL DEFAULT 0,
            fishing_next_run REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Compatibility migrations
    cur.execute("""
        DO $$
        BEGIN
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
                ALTER TABLE tg_accounts ADD COLUMN meow_next_run REAL DEFAULT 0;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='tg_accounts' AND column_name='pishi_next_run'
            ) THEN
                ALTER TABLE tg_accounts ADD COLUMN pishi_next_run REAL DEFAULT 0;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='tg_accounts' AND column_name='fishing_next_run'
            ) THEN
                ALTER TABLE tg_accounts ADD COLUMN fishing_next_run REAL DEFAULT 0;
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

    conn.commit()

    admin_user = os.getenv("ADMIN_USERNAME")
    admin_pass = os.getenv("ADMIN_PASSWORD")

    if admin_user and admin_pass:
        if not get_web_user(admin_user):
            create_web_user(admin_user, admin_pass, is_admin=True)
            print(f"✅ Created admin user: {admin_user}")


# -------------------------
# Settings
# -------------------------

def get_setting(key, default=None):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
    row = cur.fetchone()

    cur.close()

    if row:
        return row["value"]

    return DEFAULT_SETTINGS.get(key, default)


def get_all_settings():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT key, value FROM settings")
    rows = cur.fetchall()

    cur.close()

    data = DEFAULT_SETTINGS.copy()

    for row in rows:
        data[row["key"]] = row["value"]

    return data


def set_setting(key, value):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO settings (key, value)
        VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value
        """,
        (key, str(value))
    )

    conn.commit()
    cur.close()


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


# -------------------------
# Web users
# -------------------------

def create_web_user(username, password, is_admin=False):
    conn = get_conn()
    cur = conn.cursor()

    pw_hash = generate_password_hash(password)

    cur.execute(
        "INSERT INTO web_users (username, password_hash, is_admin) VALUES (%s, %s, %s)",
        (username, pw_hash, 1 if is_admin else 0)
    )

    conn.commit()
    cur.close()


def get_web_user(username):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM web_users WHERE username = %s", (username,))
    row = cur.fetchone()

    cur.close()

    return dict(row) if row else None


def verify_web_user(username, password):
    user = get_web_user(username)

    if user and check_password_hash(user["password_hash"], password):
        return user

    return None


# -------------------------
# Invites
# -------------------------

def create_invite(code):
    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute("INSERT INTO invites (code) VALUES (%s)", (code,))
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        cur.close()


def get_all_invites():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM invites ORDER BY created_at DESC")
    rows = [dict(row) for row in cur.fetchall()]

    cur.close()

    return rows


def get_valid_invite(code):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM invites WHERE code = %s AND used = 0", (code,))
    row = cur.fetchone()

    cur.close()

    return dict(row) if row else None


def use_invite(code, user_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "UPDATE invites SET used = 1, used_by_user_id = %s WHERE code = %s",
        (user_id, code)
    )

    conn.commit()
    cur.close()


# -------------------------
# Telegram accounts
# -------------------------

def _json_dumps(value):
    return json.dumps(value or [], ensure_ascii=False)


def _json_loads(value):
    try:
        return json.loads(value or "[]")
    except:
        return []


def _bool(value):
    return 1 if value else 0


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

    acc["meow_next_run"] = float(acc.get("meow_next_run") or 0.0)
    acc["pishi_next_run"] = float(acc.get("pishi_next_run") or 0.0)
    acc["fishing_next_run"] = float(acc.get("fishing_next_run") or 0.0)

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
            meow_next_run = existing.get("meow_next_run", 0.0)

        if pishi_next_run is None:
            pishi_next_run = existing.get("pishi_next_run", 0.0)

        if fishing_next_run is None:
            fishing_next_run = existing.get("fishing_next_run", 0.0)
    else:
        if meow_next_run is None:
            meow_next_run = 0.0

        if pishi_next_run is None:
            pishi_next_run = 0.0

        if fishing_next_run is None:
            fishing_next_run = 0.0

    conn = get_conn()
    cur = conn.cursor()

    encrypted_session = encrypt_session(session_string)

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
            float(meow_next_run or 0.0),
            float(pishi_next_run or 0.0),
            float(fishing_next_run or 0.0)
        )
    )

    conn.commit()
    cur.close()


def get_tg_account(phone):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM tg_accounts WHERE phone = %s", (str(phone),))
    row = cur.fetchone()

    cur.close()

    return _row_to_account(row)


def get_tg_accounts_for_user(owner_id):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT * FROM tg_accounts WHERE owner_id = %s ORDER BY created_at DESC",
        (owner_id,)
    )

    rows = [_row_to_account(row) for row in cur.fetchall()]

    cur.close()

    return rows


def get_all_tg_accounts():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM tg_accounts")

    rows = [_row_to_account(row) for row in cur.fetchall()]

    cur.close()

    return rows


def delete_tg_account(phone, owner_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM tg_accounts WHERE phone = %s AND owner_id = %s",
        (str(phone), owner_id)
    )

    conn.commit()
    cur.close()


def update_account_next_run(
    phone,
    meow_next_run=None,
    pishi_next_run=None,
    fishing_next_run=None
):
    conn = get_conn()
    cur = conn.cursor()

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
            meow_next_run,
            pishi_next_run,
            fishing_next_run,
            str(phone)
        )
    )

    conn.commit()
    cur.close()