import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL


def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                shop TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                scope TEXT,
                installed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                shop TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)


# 🔐 SALVA TOKEN
def save_shop_token(shop: str, access_token: str, scope: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO shops (shop, access_token, scope)
            VALUES (%s, %s, %s)
            ON CONFLICT (shop)
            DO UPDATE SET
                access_token = EXCLUDED.access_token,
                scope = EXCLUDED.scope,
                installed_at = CURRENT_TIMESTAMP;
            """, (shop, access_token, scope))


# 🔍 LEGGI TOKEN
def get_shop_token(shop: str):
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT access_token FROM shops WHERE shop = %s",
                (shop,)
            )
            row = cur.fetchone()
            return row["access_token"] if row else None


# 🔄 STATE OAuth
def save_oauth_state(state: str, shop: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO oauth_states (state, shop)
            VALUES (%s, %s)
            ON CONFLICT (state) DO NOTHING;
            """, (state, shop))


def consume_oauth_state(state: str, shop: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM oauth_states WHERE state = %s AND shop = %s",
                (state, shop)
            )
            row = cur.fetchone()

            if not row:
                return False

            cur.execute(
                "DELETE FROM oauth_states WHERE state = %s",
                (state,)
            )
            return True


# 🧹 RIMUOVI SHOP (webhook uninstall)
def delete_shop(shop: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM shops WHERE shop = %s",
                (shop,)
            )
