import json
import mimetypes
import time

import requests
from fastapi import HTTPException, UploadFile

from app.shopify import graphql


GALLERY_NAMESPACE = "custom"
GALLERY_KEY = "gallery"


def _assert_no_user_errors(payload: dict, path: str) -> None:
    cursor = payload.get("data", {})
    for part in path.split("."):
        cursor = cursor.get(part, {})
    user_errors = cursor.get("userErrors") or cursor.get("mediaUserErrors") or []
    if user_errors:
        raise HTTPException(status_code=400, detail=user_errors)


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
    data = graphql(
        shop=shop,
        query=query,
        variables={"first": first, "after": after},
    )

    products = []
    payload = data.get("data", {}).get("products", {})
    for edge in payload.get("edges", []):
        node = edge["node"]
        gallery_refs = node.get("metafield", {}).get("references", {}).get("nodes", [])

        gallery = []
        for ref in gallery_refs:
            image = ref.get("image") or {}
            if image.get("url"):
                gallery.append(
                    {
                        "id": ref["id"],
                        "url": image["url"],
                        "alt": ref.get("alt"),
                        "file_status": ref.get("fileStatus"),
                    }
                )

        products.append(
            {
                "id": node["id"],
                "title": node["title"],
                "cursor": edge["cursor"],
                "media": [
                    {
                        "id": media["id"],
                        "alt": media.get("alt"),
                        "type": media.get("mediaContentType"),
                        "url": (media.get("image") or {}).get("url"),
                        "file_status": media.get("fileStatus"),
                    }
                    for media in node.get("media", {}).get("nodes", [])
                    if media.get("mediaContentType") == "IMAGE" and (media.get("image") or {}).get("url")
                ],
                "gallery": gallery,
            }
        )

    return {
        "products": products,
        "page_info": payload.get("pageInfo", {"hasNextPage": False, "endCursor": None}),
    }


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
    data = graphql(
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
    target = data["data"]["stagedUploadsCreate"]["stagedTargets"][0]
    return target


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
            detail=f"Staged upload failed: {response.status_code} - {response.text[:300]}",
        )


def file_create_from_resource(shop: str, resource_url: str, alt: str | None = None) -> dict:
    query = """
    mutation CreateFileFromResource($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files {
          id
          alt
          fileStatus
          ... on MediaImage {
            image {
              url
            }
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    data = graphql(
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
          alt
          preview {
            image {
              url
            }
          }
        }
      }
    }
    """
    started = time.time()
    while time.time() - started < timeout_seconds:
        data = graphql(shop=shop, query=query, variables={"id": file_id})
        node = data.get("data", {}).get("node")
        if not node:
            raise HTTPException(status_code=400, detail="File not found after create")

        status = node.get("fileStatus")
        if status == "READY":
            return node
        if status == "FAILED":
            raise HTTPException(status_code=400, detail="Shopify failed to process the uploaded file")
        time.sleep(1.0)

    raise HTTPException(status_code=400, detail="Timed out waiting for Shopify file processing")


def attach_file_to_product(shop: str, product_id: str, file_id: str, alt: str | None = None) -> dict:
    query = """
    mutation AttachFileToProduct($input: ProductSetInput!) {
      productSet(input: $input, synchronous: true) {
        product {
          id
          media(first: 20) {
            nodes {
              id
              alt
              mediaContentType
              ... on MediaImage {
                image {
                  url
                }
              }
            }
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    data = graphql(
        shop=shop,
        query=query,
        variables={
            "input": {
                "id": product_id,
                "files": [
                    {
                        "originalSource": file_id,
                        "contentType": "IMAGE",
                        "alt": alt or "",
                    }
                ],
            }
        },
    )
    _assert_no_user_errors(data, "productSet")
    return data["data"]["productSet"]["product"]


async def upload_files_to_product(shop: str, product_id: str, files: list[UploadFile]) -> list[dict]:
    uploaded = []

    for file in files:
        binary = await file.read()
        if not binary:
            continue

        mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "image/jpeg"
        staged_target = staged_upload_create(
            shop=shop,
            filename=file.filename or "upload.jpg",
            mime_type=mime_type,
            file_size=len(binary),
        )
        upload_binary_to_staged_target(staged_target, binary)
        created_file = file_create_from_resource(shop=shop, resource_url=staged_target["resourceUrl"], alt=file.filename)
        ready = wait_until_file_ready(shop=shop, file_id=created_file["id"])
        attach_file_to_product(shop=shop, product_id=product_id, file_id=created_file["id"], alt=file.filename)

        uploaded.append(
            {
                "file_id": created_file["id"],
                "url": ready.get("preview", {}).get("image", {}).get("url"),
                "alt": file.filename,
            }
        )

    return uploaded


def get_gallery_file_ids(shop: str, product_id: str) -> list[str]:
    query = """
    query GalleryValue($id: ID!) {
      product(id: $id) {
        id
        metafield(namespace: "custom", key: "gallery") {
          type
          value
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
    data = graphql(shop=shop, query=query, variables={"id": product_id})
    metafield = data.get("data", {}).get("product", {}).get("metafield")
    if not metafield:
        return []

    refs = [node["id"] for node in metafield.get("references", {}).get("nodes", []) if node.get("id")]
    if refs:
        return refs

    value = metafield.get("value")
    if not value:
        return []

    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def set_gallery_file_ids(shop: str, product_id: str, file_ids: list[str]) -> list[str]:
    query = """
    mutation SetGallery($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        metafields {
          key
          namespace
          type
          value
        }
        userErrors {
          field
          message
          code
        }
      }
    }
    """
    data = graphql(
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


def add_file_to_gallery(shop: str, product_id: str, file_id: str) -> list[str]:
    existing = get_gallery_file_ids(shop=shop, product_id=product_id)
    if file_id not in existing:
        existing.append(file_id)
    return set_gallery_file_ids(shop=shop, product_id=product_id, file_ids=existing)


def remove_file_from_gallery(shop: str, product_id: str, file_id: str) -> list[str]:
    existing = get_gallery_file_ids(shop=shop, product_id=product_id)
    existing = [item for item in existing if item != file_id]
    return set_gallery_file_ids(shop=shop, product_id=product_id, file_ids=existing)


def delete_product_media(shop: str, product_id: str, media_id: str) -> list[str]:
    query = """
    mutation DeleteProductMedia($productId: ID!, $mediaIds: [ID!]!) {
      productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
        deletedMediaIds
        deletedProductImageIds
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
    data = graphql(
        shop=shop,
        query=query,
        variables={"productId": product_id, "mediaIds": [media_id]},
    )
    errors = data.get("data", {}).get("productDeleteMedia", {}).get("mediaUserErrors", [])
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    return data["data"]["productDeleteMedia"].get("deletedMediaIds", [])
