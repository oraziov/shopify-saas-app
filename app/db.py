import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse

from app.config import DATABASE_URL


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

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

    conn.commit()
    cur.close()
    conn.close()


# 🔐 SALVA TOKEN
def save_shop_token(shop: str, access_token: str, scope: str = None):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO shops (shop, access_token, scope)
    VALUES (%s, %s, %s)
    ON CONFLICT (shop)
    DO UPDATE SET
        access_token = EXCLUDED.access_token,
        scope = EXCLUDED.scope,
        installed_at = CURRENT_TIMESTAMP;
    """, (shop, access_token, scope))

    conn.commit()
    cur.close()
    conn.close()


# 🔍 LEGGI TOKEN
def get_shop_token(shop: str):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT access_token FROM shops WHERE shop = %s", (shop,))
    row = cur.fetchone()

    cur.close()
    conn.close()

    return row["access_token"] if row else None


# 🔄 STATE OAuth
def save_oauth_state(state: str, shop: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO oauth_states (state, shop)
    VALUES (%s, %s)
    ON CONFLICT (state) DO NOTHING;
    """, (state, shop))

    conn.commit()
    cur.close()
    conn.close()


def consume_oauth_state(state: str, shop: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT state FROM oauth_states WHERE state = %s AND shop = %s",
        (state, shop)
    )

    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return False

    cur.execute("DELETE FROM oauth_states WHERE state = %s", (state,))
    conn.commit()

    cur.close()
    conn.close()
    return True


# 🧹 RIMUOVI SHOP (webhook uninstall)
def delete_shop(shop: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("DELETE FROM shops WHERE shop = %s", (shop,))

    conn.commit()
    cur.close()
    conn.close()
