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

    response = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=timeout,
    )

    logger.debug("Shopify API call", extra={"shop": shop, "status": response.status_code, "url": url})

    if response.status_code != 200:
        logger.error("Shopify API error", extra={"shop": shop, "status": response.status_code, "response": response.text})
        raise ShopifyAPIError(
            f"HTTP {response.status_code} - {response.text}"
        )

    try:
        data = response.json()
    except Exception as e:
        logger.error("Failed to parse Shopify API response as JSON", extra={"shop": shop, "response": response.text, "error": str(e)})
        raise ShopifyAPIError(f"Failed to parse JSON response: {e}")

    if "errors" in data:
        logger.error("Shopify GraphQL errors", extra={"shop": shop, "errors": data["errors"]})
        raise ShopifyAPIError(f"GraphQL errors: {data['errors']}")

    return data