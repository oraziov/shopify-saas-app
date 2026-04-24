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

    print("STATUS:", response.status_code)
    print("RESPONSE TEXT:", response.text)

    try:
        data = response.json()
    except Exception as e:
        print("JSON ERROR:", e)
        raise ShopifyAPIError(f"Failed to parse JSON response: {e}")

    print("DATA:", data)
    print("------ END DEBUG ------")

    if response.status_code != 200:
        raise ShopifyAPIError(
            f"HTTP {response.status_code} - {response.text}"
        )

    if "errors" in data:
        raise ShopifyAPIError(f"GraphQL errors: {data['errors']}")

    return data