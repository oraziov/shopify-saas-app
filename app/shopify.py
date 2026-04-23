import logging
import time

import requests

from app.config import API_VERSION
from app.db import get_shop_token

logger = logging.getLogger(__name__)


class ShopifyAPIError(Exception):
    pass


def graphql(
    *,
    shop: str,
    query: str,
    variables: dict | None = None,
    timeout: int = 30,
    max_retries: int = 3,
) -> dict:
    if not shop:
        raise ValueError("shop is required")

    access_token = get_shop_token(shop)
    if not access_token:
        raise ValueError(f"No access token found for shop: {shop}")

    url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"query": query, "variables": variables or {}}

    backoff = 1.0
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )

            if response.status_code == 429:
                logger.warning("Shopify rate limited request", extra={"shop": shop, "attempt": attempt})
                time.sleep(backoff)
                backoff *= 2
                continue

            if response.status_code >= 500:
                logger.warning("Shopify server error", extra={"shop": shop, "attempt": attempt})
                time.sleep(backoff)
                backoff *= 2
                continue

            if response.status_code != 200:
                raise ShopifyAPIError(f"HTTP {response.status_code} - {response.text}")

            data = response.json()

            if "errors" in data:
                raise ShopifyAPIError(f"GraphQL top-level errors: {data['errors']}")

            throttle = data.get("extensions", {}).get("cost", {}).get("throttleStatus", {})
            currently_available = throttle.get("currentlyAvailable")
            restore_rate = throttle.get("restoreRate")
            if currently_available is not None and restore_rate is not None and currently_available < 50:
                time.sleep(min(max(0.5, 50 / max(restore_rate, 1)), 2.0))

            return data

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            logger.warning("Transient Shopify network error", extra={"shop": shop, "attempt": attempt, "error": str(exc)})
            if attempt == max_retries:
                break
            time.sleep(backoff)
            backoff *= 2

    raise ShopifyAPIError(f"Shopify request failed after retries: {last_error}")
