import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import urlencode


SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")


def is_valid_shop_domain(shop: str) -> bool:
    return bool(SHOP_RE.fullmatch(shop or ""))


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def verify_shopify_hmac(params: dict[str, str], provided_hmac: str, client_secret: str) -> bool:
    filtered = {
        k: v for k, v in params.items()
        if k not in {"hmac", "signature"} and v is not None
    }
    message = urlencode(sorted(filtered.items()), doseq=True)
    digest = hmac.new(
        client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, provided_hmac)


def verify_webhook_hmac(raw_body: bytes, provided_hmac: str | None, client_secret: str) -> bool:
    if not provided_hmac:
        return False
    digest = hmac.new(
        client_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, provided_hmac)
