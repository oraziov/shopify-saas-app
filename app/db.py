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
            cur.execute("""
CREATE TABLE IF NOT EXISTS csv_catalog (
    id SERIAL PRIMARY KEY,
    shop TEXT NOT NULL,
    handle TEXT,
    brand TEXT,
    title TEXT,
    sku TEXT,
    colore TEXT,
    taglia TEXT,
    color_code TEXT,
    image1 TEXT,
    image2 TEXT,
    image3 TEXT,
    raw JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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