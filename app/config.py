import os
from dotenv import load_dotenv

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "production")
APP_URL = os.getenv("APP_URL", "").rstrip("/")

SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_SCOPES = os.getenv(
    "SHOPIFY_SCOPES",
    "read_products,read_files,read_metaobjects"
)
API_VERSION = os.getenv("API_VERSION", "2026-04")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./shopify.db")
APP_SECRET = os.getenv("APP_SECRET", "change-me")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

EMBEDDED_APP = os.getenv("EMBEDDED_APP", "false").lower() == "true"
