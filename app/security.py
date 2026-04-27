import base64
import hashlib
import hmac
import re
import secrets
import time

import jwt
from fastapi import HTTPException

from app.config import SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET

SHOP_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]*\.myshopify\.com$")

CSRF_TTL_SECONDS = 60 * 60


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def is_valid_shop_domain(shop: str) -> bool:
    return bool(shop and SHOP_DOMAIN_RE.match(shop))


def verify_shopify_hmac(params: dict, hmac_value: str) -> bool:
    filtered = {
        key: value
        for key, value in params.items()
        if key not in ["hmac", "signature"]
    }

    message = "&".join(
        f"{key}={value}"
        for key, value in sorted(filtered.items())
    )

    digest = hmac.new(
        SHOPIFY_CLIENT_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(digest, hmac_value)


def verify_webhook_hmac(raw_body: bytes, hmac_header: str | None) -> bool:
    if not hmac_header:
        return False

    digest = hmac.new(
        SHOPIFY_CLIENT_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).digest()

    computed = base64.b64encode(digest).decode("utf-8")

    return hmac.compare_digest(computed, hmac_header)


def verify_shopify_session_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            SHOPIFY_CLIENT_SECRET,
            algorithms=["HS256"],
            audience=SHOPIFY_CLIENT_ID,
            options={
                "require": ["exp", "iat", "iss", "dest", "aud"],
            },
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session token")

    dest = payload.get("dest")
    iss = payload.get("iss")

    if not dest or not dest.startswith("https://"):
        raise HTTPException(status_code=401, detail="Invalid token destination")

    shop = dest.replace("https://", "").strip("/")

    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=401, detail="Invalid token shop")

    if iss and shop not in iss:
        raise HTTPException(status_code=401, detail="Invalid token issuer")

    payload["shop"] = shop

    return payload


def create_csrf_token(shop: str) -> str:
    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)

    payload = f"{shop}:{timestamp}:{nonce}"

    signature = hmac.new(
        SHOPIFY_CLIENT_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    raw_token = f"{payload}:{signature}"

    return base64.urlsafe_b64encode(raw_token.encode("utf-8")).decode("utf-8")


def require_csrf_token(token: str | None, shop: str) -> None:
    if not token:
        raise HTTPException(status_code=403, detail="Missing CSRF token")

    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        token_shop, timestamp, nonce, signature = decoded.split(":", 3)
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    if token_shop != shop:
        raise HTTPException(status_code=403, detail="Invalid CSRF shop")

    age = int(time.time()) - int(timestamp)
    if age > CSRF_TTL_SECONDS:
        raise HTTPException(status_code=403, detail="Expired CSRF token")

    payload = f"{token_shop}:{timestamp}:{nonce}"

    expected_signature = hmac.new(
        SHOPIFY_CLIENT_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=403, detail="Invalid CSRF signature")