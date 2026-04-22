import sqlite3
from contextlib import contextmanager
from urllib.parse import urlparse


def _sqlite_path(database_url: str) -> str:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        raise ValueError(
            "This starter package uses SQLite. "
            "Set DATABASE_URL=sqlite:///./shopify.db or replace db.py with Postgres."
        )
    return parsed.path if parsed.path else "./shopify.db"


@contextmanager
def get_conn(db_path: str):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str):
    with get_conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS shops (
                shop TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                scope TEXT,
                installed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                shop TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def save_shop_token(db_path: str, shop: str, access_token: str, scope: str | None = None):
    with get_conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shops (shop, access_token, scope)
            VALUES (?, ?, ?)
            ON CONFLICT(shop) DO UPDATE SET
                access_token = excluded.access_token,
                scope = excluded.scope,
                installed_at = CURRENT_TIMESTAMP
            """,
            (shop, access_token, scope),
        )
        conn.commit()


def get_shop_token(db_path: str, shop: str) -> str | None:
    with get_conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT access_token FROM shops WHERE shop = ?", (shop,))
        row = cur.fetchone()
        return row["access_token"] if row else None


def delete_shop(db_path: str, shop: str):
    with get_conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM shops WHERE shop = ?", (shop,))
        conn.commit()


def save_oauth_state(db_path: str, state: str, shop: str):
    with get_conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO oauth_states (state, shop)
            VALUES (?, ?)
            """,
            (state, shop),
        )
        conn.commit()


def consume_oauth_state(db_path: str, state: str, shop: str) -> bool:
    with get_conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT state FROM oauth_states WHERE state = ? AND shop = ?",
            (state, shop),
        )
        row = cur.fetchone()
        if not row:
            return False

        cur.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        conn.commit()
        return True
