import psycopg

from app.config import DATABASE_URL


def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                shop TEXT PRIMARY KEY,
                access_token TEXT NOT NULL
            );
            """)


def save_shop_token(shop: str, token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO shops (shop, access_token)
            VALUES (%s, %s)
            ON CONFLICT (shop)
            DO UPDATE SET access_token = EXCLUDED.access_token;
            """, (shop, token))


def get_shop_token(shop: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT access_token FROM shops WHERE shop=%s", (shop,))
            row = cur.fetchone()
            return row[0] if row else None