import json
from app.shopify import graphql


def resolve_media(shop: str, ids: list[str], access_token: str, api_version: str) -> list[str]:
    if not ids:
        return []

    query = """
    query getMedia($ids: [ID!]!) {
      nodes(ids: $ids) {
        ... on MediaImage {
          image {
            url
          }
        }
      }
    }
    """

    data = graphql(
        shop=shop,
        query=query,
        variables={"ids": ids},
        access_token=access_token,
        api_version=api_version,
    )

    images = []
    for node in data.get("data", {}).get("nodes", []):
        if node and node.get("image") and node["image"].get("url"):
            images.append(node["image"]["url"])

    return images


def get_products_full(
    *,
    limit: int,
    shop: str,
    access_token: str,
    api_version: str,
) -> list[dict]:
    query = """
    query getProducts($first: Int!) {
      products(first: $first) {
        edges {
          node {
            id
            title
            images(first: 5) {
              edges {
                node {
                  url
                }
              }
            }
            metafield(namespace: "custom", key: "gallery") {
              value
            }
          }
        }
      }
    }
    """

    data = graphql(
        shop=shop,
        query=query,
        variables={"first": limit},
        access_token=access_token,
        api_version=api_version,
    )

    products = []

    for edge in data.get("data", {}).get("products", {}).get("edges", []):
        p = edge["node"]

        images = [
            img["node"]["url"]
            for img in p.get("images", {}).get("edges", [])
            if img.get("node", {}).get("url")
        ]

        gallery_ids = []
        metafield = p.get("metafield")
        if metafield and metafield.get("value"):
            try:
                gallery_ids = json.loads(metafield["value"])
                if not isinstance(gallery_ids, list):
                    gallery_ids = []
            except (json.JSONDecodeError, TypeError):
                gallery_ids = []

        gallery_images = resolve_media(
            shop=shop,
            ids=gallery_ids,
            access_token=access_token,
            api_version=api_version,
        )

        products.append(
            {
                "id": p["id"],
                "title": p["title"],
                "images": images,
                "gallery_images": gallery_images,
            }
        )

    return products
