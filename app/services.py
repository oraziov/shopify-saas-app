import json
import mimetypes
import time

import requests
from fastapi import HTTPException, UploadFile

from app.shopify import graphql


GALLERY_NAMESPACE = "custom"
GALLERY_KEY = "gallery"


# 🔥 SAFE GRAPHQL WRAPPER
def safe_graphql(shop: str, query: str, variables: dict | None = None):
    data = graphql(shop=shop, query=query, variables=variables)

    if not data:
        raise HTTPException(status_code=500, detail="GraphQL returned None")

    if "errors" in data:
        raise HTTPException(status_code=400, detail=data["errors"])

    if "data" not in data:
        raise HTTPException(status_code=500, detail=f"Invalid response: {data}")

    return data


def _assert_no_user_errors(payload: dict, path: str) -> None:
    cursor = payload.get("data", {})
    for part in path.split("."):
        cursor = cursor.get(part, {})
    user_errors = cursor.get("userErrors") or cursor.get("mediaUserErrors") or []
    if user_errors:
        raise HTTPException(status_code=400, detail=user_errors)


# ===============================
# PRODUCTS
# ===============================

def get_products_page(shop: str, first: int = 12, after: str | None = None) -> dict:
    query = """
    query ProductsPage($first: Int!, $after: String) {
      products(first: $first, after: $after, sortKey: UPDATED_AT, reverse: true) {
        pageInfo {
          hasNextPage
          endCursor
        }
        edges {
          cursor
          node {
            id
            title
            media(first: 20) {
              nodes {
                id
                alt
                mediaContentType
                ... on MediaImage {
                  fileStatus
                  image {
                    url
                  }
                }
              }
            }
            metafield(namespace: "custom", key: "gallery") {
              id
              type
              value
              references(first: 50) {
                nodes {
                  ... on MediaImage {
                    id
                    alt
                    fileStatus
                    image {
                      url
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    data = safe_graphql(
        shop=shop,
        query=query,
        variables={"first": first, "after": after},
    )

    payload = data["data"]["products"]

    products = []
    for edge in payload.get("edges", []):
        node = edge["node"]

        gallery_refs = (
            node.get("metafield", {})
            .get("references", {})
            .get("nodes", [])
        )

        gallery = [
            {
                "id": ref["id"],
                "url": (ref.get("image") or {}).get("url"),
                "alt": ref.get("alt"),
                "file_status": ref.get("fileStatus"),
            }
            for ref in gallery_refs
            if (ref.get("image") or {}).get("url")
        ]

        media = [
            {
                "id": m["id"],
                "alt": m.get("alt"),
                "type": m.get("mediaContentType"),
                "url": (m.get("image") or {}).get("url"),
                "file_status": m.get("fileStatus"),
            }
            for m in node.get("media", {}).get("nodes", [])
            if m.get("mediaContentType") == "IMAGE"
            and (m.get("image") or {}).get("url")
        ]

        products.append(
            {
                "id": node["id"],
                "title": node["title"],
                "cursor": edge["cursor"],
                "media": media,
                "gallery": gallery,
            }
        )

    return {
        "products": products,
        "page_info": payload.get("pageInfo", {}),
    }


# ===============================
# UPLOAD
# ===============================

def staged_upload_create(shop: str, filename: str, mime_type: str, file_size: int) -> dict:
    query = """
    mutation CreateStagedUploads($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets {
          url
          resourceUrl
          parameters {
            name
            value
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    data = safe_graphql(
        shop=shop,
        query=query,
        variables={
            "input": [
                {
                    "filename": filename,
                    "mimeType": mime_type,
                    "resource": "IMAGE",
                    "fileSize": str(file_size),
                    "httpMethod": "PUT",
                }
            ]
        },
    )

    _assert_no_user_errors(data, "stagedUploadsCreate")

    return data["data"]["stagedUploadsCreate"]["stagedTargets"][0]


def upload_binary_to_staged_target(staged_target: dict, binary: bytes) -> None:
    params = staged_target.get("parameters") or []
    headers = {item["name"]: item["value"] for item in params}

    response = requests.put(
        staged_target["url"],
        data=binary,
        headers=headers,
        timeout=120,
    )

    if response.status_code not in (200, 201):
        raise HTTPException(
            status_code=400,
            detail=f"Upload failed: {response.text[:200]}",
        )


def file_create_from_resource(shop: str, resource_url: str, alt: str | None = None) -> dict:
    query = """
    mutation CreateFile($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          id
          fileStatus
          ... on MediaImage {
            image {
              url
            }
          }
        }
        userErrors {
          message
        }
      }
    }
    """

    data = safe_graphql(
        shop=shop,
        query=query,
        variables={
            "files": [
                {
                    "originalSource": resource_url,
                    "contentType": "IMAGE",
                    "alt": alt or "",
                }
            ]
        },
    )

    _assert_no_user_errors(data, "fileCreate")

    return data["data"]["fileCreate"]["files"][0]


def wait_until_file_ready(shop: str, file_id: str, timeout_seconds: int = 60) -> dict:
    query = """
    query FileStatus($id: ID!) {
      node(id: $id) {
        ... on File {
          id
          fileStatus
          preview {
            image {
              url
            }
          }
        }
      }
    }
    """

    start = time.time()

    while time.time() - start < timeout_seconds:
        data = safe_graphql(shop=shop, query=query, variables={"id": file_id})

        node = data["data"]["node"]

        if node["fileStatus"] == "READY":
            return node

        if node["fileStatus"] == "FAILED":
            raise HTTPException(status_code=400, detail="File processing failed")

        time.sleep(1)

    raise HTTPException(status_code=400, detail="Timeout waiting file")


def attach_file_to_product(shop: str, product_id: str, file_id: str):
    query = """
    mutation AttachFile($input: ProductSetInput!) {
      productSet(input: $input, synchronous: true) {
        product {
          id
        }
        userErrors {
          message
        }
      }
    }
    """

    data = safe_graphql(
        shop=shop,
        query=query,
        variables={
            "input": {
                "id": product_id,
                "files": [
                    {
                        "originalSource": file_id,
                        "contentType": "IMAGE",
                    }
                ],
            }
        },
    )

    _assert_no_user_errors(data, "productSet")

    return True


async def upload_files_to_product(shop: str, product_id: str, files: list[UploadFile]) -> list[dict]:
    results = []

    for file in files:
        binary = await file.read()

        mime = file.content_type or "image/jpeg"

        staged = staged_upload_create(shop, file.filename, mime, len(binary))
        upload_binary_to_staged_target(staged, binary)

        created = file_create_from_resource(shop, staged["resourceUrl"], file.filename)
        ready = wait_until_file_ready(shop, created["id"])

        attach_file_to_product(shop, product_id, created["id"])

        results.append({
            "id": created["id"],
            "url": ready["preview"]["image"]["url"],
        })

    return results


# ===============================
# GALLERY
# ===============================

def get_gallery_file_ids(shop: str, product_id: str) -> list[str]:
    query = """
    query GetGallery($id: ID!) {
      product(id: $id) {
        metafield(namespace: "custom", key: "gallery") {
          references(first: 100) {
            nodes {
              ... on MediaImage {
                id
              }
            }
          }
        }
      }
    }
    """

    data = safe_graphql(shop=shop, query=query, variables={"id": product_id})

    nodes = (
        data["data"]["product"]["metafield"]
        .get("references", {})
        .get("nodes", [])
    )

    return [n["id"] for n in nodes if n.get("id")]


def set_gallery_file_ids(shop: str, product_id: str, file_ids: list[str]) -> list[str]:
    query = """
    mutation SetGallery($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        userErrors {
          message
        }
      }
    }
    """

    data = safe_graphql(
        shop=shop,
        query=query,
        variables={
            "metafields": [
                {
                    "ownerId": product_id,
                    "namespace": GALLERY_NAMESPACE,
                    "key": GALLERY_KEY,
                    "type": "list.file_reference",
                    "value": json.dumps(file_ids),
                }
            ]
        },
    )

    _assert_no_user_errors(data, "metafieldsSet")

    return file_ids


def add_file_to_gallery(shop: str, product_id: str, file_id: str):
    ids = get_gallery_file_ids(shop, product_id)

    if file_id not in ids:
        ids.append(file_id)

    return set_gallery_file_ids(shop, product_id, ids)


def remove_file_from_gallery(shop: str, product_id: str, file_id: str):
    ids = get_gallery_file_ids(shop, product_id)

    ids = [i for i in ids if i != file_id]

    return set_gallery_file_ids(shop, product_id, ids)

def delete_product_media(shop: str, product_id: str, media_id: str) -> list[str]:
    query = """
    mutation DeleteProductMedia($productId: ID!, $mediaIds: [ID!]!) {
      productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
        deletedMediaIds
        mediaUserErrors {
          field
          message
        }
        product {
          id
        }
      }
    }
    """

    data = safe_graphql(
        shop=shop,
        query=query,
        variables={
            "productId": product_id,
            "mediaIds": [media_id],
        },
    )

    errors = (
        data["data"]
        .get("productDeleteMedia", {})
        .get("mediaUserErrors", [])
    )

    if errors:
        raise HTTPException(status_code=400, detail=errors)

    return (
        data["data"]
        .get("productDeleteMedia", {})
        .get("deletedMediaIds", [])
    )