import sqlite3
import threading
import json
import os
from werkzeug.security import generate_password_hash, check_password_hash
from config import DB_PATH

_local = threading.local()

def get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return _local.conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            is_active INTEGER DEFAULT 0,
            cached_groups TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES web_users(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    
    # Auto-create admin
    admin_user = os.getenv("ADMIN_USERNAME")
    admin_pass = os.getenv("ADMIN_PASSWORD")
    if admin_user and admin_pass:
        if not get_web_user(admin_user):
            create_web_user(admin_user, admin_pass, is_admin=True)
            print(f"✅ Created admin user: {admin_user}")

def create_web_user(username, password, is_admin=False):
    conn = get_conn()
    pw_hash = generate_password_hash(password)
    conn.execute(
        "INSERT INTO web_users (username, password_hash, is_admin) VALUES (?, ?, ?)",
        (username, pw_hash, 1 if is_admin else 0)
    )
    conn.commit()

def get_web_user(username):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM web_users WHERE username = ?", (username,))
    row = cur.fetchone()
    return dict(row) if row else None

def verify_web_user(username, password):
    user = get_web_user(username)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None

def create_invite(code):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO invites (code) VALUES (?)", (code,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def get_all_invites():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM invites ORDER BY created_at DESC")
    return [dict(row) for row in cur.fetchall()]

def get_valid_invite(code):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM invites WHERE code = ? AND used = 0", (code,))
    row = cur.fetchone()
    return dict(row) if row else None

def use_invite(code, user_id):
    conn = get_conn()
    conn.execute("UPDATE invites SET used = 1, used_by_user_id = ? WHERE code = ?", (user_id, code))
    conn.commit()

def _json_dumps(value): return json.dumps(value or [], ensure_ascii=False)
def _json_loads(value):
    try: return json.loads(value or "[]")
    except: return []
def _bool(value): return 1 if value else 0

def _row_to_account(row):
    if not row: return None
    acc = dict(row)
    acc["selected_groups"] = _json_loads(acc.get("selected_groups"))
    acc["cached_groups"] = _json_loads(acc.get("cached_groups"))
    acc["meow_enabled"] = bool(acc.get("meow_enabled"))
    acc["pishi_enabled"] = bool(acc.get("pishi_enabled"))
    acc["is_active"] = bool(acc.get("is_active"))
    acc["fish_enabled"] = acc["pishi_enabled"]
    return acc

def save_tg_account(phone, owner_id, session_string, selected_groups=None, meow_enabled=False, pishi_enabled=False, is_active=False, cached_groups=None):
    conn = get_conn()
    conn.execute("""
        INSERT INTO tg_accounts (phone, owner_id, session_string, selected_groups, meow_enabled, pishi_enabled, is_active, cached_groups)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            owner_id = excluded.owner_id,
            session_string = excluded.session_string,
            selected_groups = excluded.selected_groups,
            meow_enabled = excluded.meow_enabled,
            pishi_enabled = excluded.pishi_enabled,
            is_active = excluded.is_active,
            cached_groups = excluded.cached_groups
    """, (str(phone), owner_id, session_string, _json_dumps(selected_groups), _bool(meow_enabled), _bool(pishi_enabled), _bool(is_active), _json_dumps(cached_groups)))
    conn.commit()

def get_tg_account(phone):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tg_accounts WHERE phone = ?", (str(phone),))
    return _row_to_account(cur.fetchone())

def get_tg_accounts_for_user(owner_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tg_accounts WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,))
    return [_row_to_account(row) for row in cur.fetchall()]

def get_all_tg_accounts():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tg_accounts")
    return [_row_to_account(row) for row in cur.fetchall()]

def delete_tg_account(phone, owner_id):
    conn = get_conn()
    conn.execute("DELETE FROM tg_accounts WHERE phone = ? AND owner_id = ?", (str(phone), owner_id))
    conn.commit()