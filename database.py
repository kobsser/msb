import os
import threading
import json
import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken # <--- NEW IMPORT

_local = threading.local()

# Initialize Fernet cipher
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if ENCRYPTION_KEY:
    cipher = Fernet(ENCRYPTION_KEY.encode())
else:
    cipher = None
    print("⚠️ Warning: ENCRYPTION_KEY not set. Session strings will NOT be encrypted.")

def encrypt_session(session_string: str) -> str:
    if not cipher or not session_string:
        return session_string or ""
    return cipher.encrypt(session_string.encode()).decode()

def decrypt_session(encrypted_string: str) -> str:
    if not cipher or not encrypted_string:
        return encrypted_string or ""
    try:
        return cipher.decrypt(encrypted_string.encode()).decode()
    except InvalidToken:
        # Fallback: If it fails to decrypt, it means it's an old plaintext string 
        # or was manually edited in Supabase. Return as-is so the bot doesn't crash.
        print(f"⚠️ Warning: Failed to decrypt session for {encrypted_string[:10]}... Returning plaintext.")
        return encrypted_string

def get_conn():
    if not hasattr(_local, "conn") or _local.conn is None or _local.conn.closed:
        db_url = os.getenv("DATABASE_URL")
        if not db_url: raise RuntimeError("DATABASE_URL environment variable is missing.")
        _local.conn = psycopg2.connect(db_url)
    else:
        try:
            cur = _local.conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        except psycopg2.OperationalError:
            db_url = os.getenv("DATABASE_URL")
            _local.conn = psycopg2.connect(db_url)
    return _local.conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS web_users (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invites (
            id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL,
            used INTEGER DEFAULT 0, used_by_user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tg_accounts (
            phone TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
            session_string TEXT NOT NULL, selected_groups TEXT DEFAULT '[]',
            meow_enabled INTEGER DEFAULT 0, pishi_enabled INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 0, cached_groups TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    
    admin_user = os.getenv("ADMIN_USERNAME")
    admin_pass = os.getenv("ADMIN_PASSWORD")
    if admin_user and admin_pass:
        if not get_web_user(admin_user):
            create_web_user(admin_user, admin_pass, is_admin=True)
            print(f"✅ Created admin user: {admin_user}")

def create_web_user(username, password, is_admin=False):
    conn = get_conn()
    pw_hash = generate_password_hash(password)
    cur = conn.cursor()
    cur.execute("INSERT INTO web_users (username, password_hash, is_admin) VALUES (%s, %s, %s)", (username, pw_hash, 1 if is_admin else 0))
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
    if user and check_password_hash(user["password_hash"], password): return user
    return None

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
    finally: cur.close()

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
    cur.execute("UPDATE invites SET used = 1, used_by_user_id = %s WHERE code = %s", (user_id, code))
    conn.commit()
    cur.close()

def _json_dumps(value): return json.dumps(value or [], ensure_ascii=False)
def _json_loads(value):
    try: return json.loads(value or "[]")
    except: return []
def _bool(value): return 1 if value else 0

def _row_to_account(row):
    if not row: return None
    acc = dict(row)
    
    # DECRYPT SESSION STRING HERE
    acc["session_string"] = decrypt_session(acc.get("session_string", ""))
    
    acc["selected_groups"] = _json_loads(acc.get("selected_groups"))
    acc["cached_groups"] = _json_loads(acc.get("cached_groups"))
    acc["meow_enabled"] = bool(acc.get("meow_enabled"))
    acc["pishi_enabled"] = bool(acc.get("pishi_enabled"))
    acc["is_active"] = bool(acc.get("is_active"))
    acc["fish_enabled"] = acc["pishi_enabled"]
    return acc

def save_tg_account(phone, owner_id, session_string, selected_groups=None, meow_enabled=False, pishi_enabled=False, is_active=False, cached_groups=None):
    conn = get_conn()
    cur = conn.cursor()
    
    # ENCRYPT SESSION STRING HERE
    encrypted_session = encrypt_session(session_string)
    
    cur.execute("""
        INSERT INTO tg_accounts (phone, owner_id, session_string, selected_groups, meow_enabled, pishi_enabled, is_active, cached_groups)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (phone) DO UPDATE SET
            owner_id = EXCLUDED.owner_id, session_string = EXCLUDED.session_string,
            selected_groups = EXCLUDED.selected_groups, meow_enabled = EXCLUDED.meow_enabled,
            pishi_enabled = EXCLUDED.pishi_enabled, is_active = EXCLUDED.is_active,
            cached_groups = EXCLUDED.cached_groups
    """, (str(phone), owner_id, encrypted_session, _json_dumps(selected_groups), _bool(meow_enabled), _bool(pishi_enabled), _bool(is_active), _json_dumps(cached_groups)))
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
    cur.execute("SELECT * FROM tg_accounts WHERE owner_id = %s ORDER BY created_at DESC", (owner_id,))
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
    cur.execute("DELETE FROM tg_accounts WHERE phone = %s AND owner_id = %s", (str(phone), owner_id))
    conn.commit()
    cur.close()