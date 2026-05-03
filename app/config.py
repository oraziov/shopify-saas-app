import os
from dotenv import load_dotenv

load_dotenv()

APP_URL = os.getenv("APP_URL", "").rstrip("/")

SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
API_VERSION = os.getenv("API_VERSION", "2026-04")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set")