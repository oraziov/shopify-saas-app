import logging
import time

import requests


logger = logging.getLogger(__name__)


class ShopifyAPIError(Exception):
    pass


def graphql(
    *,
    shop: str,
    query: str,
    variables: dict | None,
    access_token: str,
    api_version: str,
    timeout: int = 30,
    max_retries: int = 3,
) -> dict:
    if not shop:
        raise ValueError("shop is required")
    if not access_token:
        raise ValueError(f"No access token available for shop: {shop}")

    url = f"https://{shop}/admin/api/{api_version}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "query": query,
        "variables": variables or {},
    }

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
                logger.warning("Shopify server error", extra={"shop": shop, "status_code": response.status_code, "attempt": attempt})
                time.sleep(backoff)
                backoff *= 2
                continue

            if response.status_code != 200:
                raise ShopifyAPIError(f"HTTP {response.status_code} - {response.text}")

            data = response.json()

            if "errors" in data:
                raise ShopifyAPIError(f"GraphQL top-level errors: {data['errors']}")

            user_errors = []
            # Useful when you later add mutations.
            if isinstance(data.get("data"), dict):
                for value in data["data"].values():
                    if isinstance(value, dict) and value.get("userErrors"):
                        user_errors.extend(value["userErrors"])

            if user_errors:
                raise ShopifyAPIError(f"GraphQL user errors: {user_errors}")

            throttle = data.get("extensions", {}).get("cost", {}).get("throttleStatus", {})
            currently_available = throttle.get("currentlyAvailable")
            restore_rate = throttle.get("restoreRate")
            if currently_available is not None and restore_rate is not None and currently_available < 50:
                sleep_for = max(0.5, 50 / max(restore_rate, 1))
                logger.info("Approaching Shopify GraphQL throttle, sleeping briefly", extra={"shop": shop, "sleep_for": sleep_for})
                time.sleep(min(sleep_for, 2.0))

            return data

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            logger.warning("Transient Shopify network error", extra={"shop": shop, "attempt": attempt, "error": str(exc)})
            if attempt == max_retries:
                break
            time.sleep(backoff)
            backoff *= 2

    raise ShopifyAPIError(f"Shopify request failed after retries: {last_error}")
