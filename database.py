import os
import json
import sqlite3
import threading

from config import DB_PATH

_local = threading.local()


def get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn

    return _local.conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            session_string TEXT NOT NULL,
            selected_groups TEXT DEFAULT '[]',
            meow_enabled INTEGER DEFAULT 0,
            fish_enabled INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 0,
            cached_groups TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in cur.fetchall()}

    if "selected_groups" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN selected_groups TEXT DEFAULT '[]'")

    if "meow_enabled" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN meow_enabled INTEGER DEFAULT 0")

    if "fish_enabled" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN fish_enabled INTEGER DEFAULT 0")

    if "is_active" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 0")

    if "cached_groups" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN cached_groups TEXT DEFAULT '[]'")

    conn.commit()


def _bool(value):
    return 1 if value else 0


def _json_dumps(value):
    return json.dumps(value or [], ensure_ascii=False)


def _json_loads(value):
    try:
        data = json.loads(value or "[]")
        if isinstance(data, list):
            return data
        return []
    except:
        return []


def _row_to_user(row):
    if not row:
        return None

    user = dict(row)

    user["selected_groups"] = _json_loads(user.get("selected_groups"))
    user["cached_groups"] = _json_loads(user.get("cached_groups"))

    user["meow_enabled"] = bool(user.get("meow_enabled"))
    user["fish_enabled"] = bool(user.get("fish_enabled"))
    user["is_active"] = bool(user.get("is_active"))

    # Compatibility alias for templates/logic
    user["pishi_enabled"] = user["fish_enabled"]

    return user


def save_user(
    phone,
    session_string,
    selected_groups=None,
    meow_enabled=False,
    fish_enabled=False,
    is_active=False,
    cached_groups=None,
    pishi_enabled=None
):
    if pishi_enabled is not None:
        fish_enabled = pishi_enabled

    conn = get_conn()

    conn.execute(
        """
        INSERT INTO users (
            phone,
            session_string,
            selected_groups,
            meow_enabled,
            fish_enabled,
            is_active,
            cached_groups
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            session_string = excluded.session_string,
            selected_groups = excluded.selected_groups,
            meow_enabled = excluded.meow_enabled,
            fish_enabled = excluded.fish_enabled,
            is_active = excluded.is_active,
            cached_groups = excluded.cached_groups
        """,
        (
            str(phone),
            session_string,
            _json_dumps(selected_groups),
            _bool(meow_enabled),
            _bool(fish_enabled),
            _bool(is_active),
            _json_dumps(cached_groups),
        )
    )

    conn.commit()


def get_user(phone):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE phone = ?", (str(phone),))
    row = cur.fetchone()

    return _row_to_user(row)


def get_all_users():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users ORDER BY phone")
    rows = cur.fetchall()

    return [_row_to_user(row) for row in rows if row]


def delete_user(phone):
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE phone = ?", (str(phone),))
    conn.commit()