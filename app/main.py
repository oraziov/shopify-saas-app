import csv
import io
import json
import logging

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import API_VERSION
from app.db import get_shop_token, init_db
from app.security import verify_shopify_session_token

logger = logging.getLogger(__name__)

app = FastAPI(title="Shopify Image Manager")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup():
    init_db()


# ── UI ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def ui(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── AUTH helper ──────────────────────────────────────────────────────────────

def _get_authenticated_shop(authorization: str | None) -> str:
    """Extract shop from Shopify session token (Bearer)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise ValueError("Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ")
    payload = verify_shopify_session_token(token)
    return payload["shop"]


# ── CSV PARSE ────────────────────────────────────────────────────────────────

@app.post("/api/csv/parse")
async def parse_csv(file: UploadFile = File(...)):
    """
    Parse CSV and return grouped product data.
    Does NOT call Shopify — pure parsing, fast.
    """
    contents = await file.read()
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        text = contents.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    # Group rows by handle → color_code
    products: dict[str, dict] = {}

    for row in reader:
        handle = str(row.get("handle") or "").strip()
        if not handle:
            continue

        color_code = str(row.get("color_code") or "").strip()
        colore = str(row.get("Colore") or row.get("colore") or "").strip()
        sku = str(row.get("SKU") or row.get("Variant SKU") or "").strip()
        taglia = str(row.get("Taglia") or row.get("taglia") or "").strip()

        images = [
            row.get("Image1") or "",
            row.get("Image2") or "",
            row.get("Image3") or "",
        ]
        images = [i.strip() for i in images if i.strip()]

        key = f"{handle}__{color_code}"

        if key not in products:
            products[key] = {
                "handle": handle,
                "brand": str(row.get("Brand") or "").strip(),
                "title": str(row.get("Title") or "").strip(),
                "colore": colore,
                "color_code": color_code,
                "images_csv": images,
                "variants": [],
            }

        products[key]["variants"].append({"sku": sku, "taglia": taglia})

        # keep unique images (same URLs repeated across variant rows)
        for img in images:
            if img and img not in products[key]["images_csv"]:
                products[key]["images_csv"].append(img)

    return list(products.values())


# ── SHOPIFY: fetch product media ─────────────────────────────────────────────

@app.get("/api/shopify/product")
async def get_shopify_product(shop: str, handle: str, sku: str = ""):
    """
    Fetch product + media from Shopify.
    Tries productByHandle first; if not found (numeric handle from CSV),
    falls back to searching by SKU via the products query.
    """
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Shop not authenticated"})

    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }
    url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"

    MEDIA_FRAGMENT = """
        id
        title
        media(first: 30) {
          nodes {
            id
            mediaContentType
            ... on MediaImage {
              fileStatus
              image { url }
              alt
            }
          }
        }
    """

    async with httpx.AsyncClient(timeout=30) as client:

        # 1️⃣ Try productByHandle (works when handle is a real slug)
        resp = await client.post(url, headers=headers, json={
            "query": f"query ($h: String!) {{ productByHandle(handle: $h) {{ {MEDIA_FRAGMENT} }} }}",
            "variables": {"h": handle},
        })
        product = (resp.json().get("data") or {}).get("productByHandle")

        # 2️⃣ Fallback: search by SKU (handles numeric codes like 330650)
        if not product and sku:
            resp2 = await client.post(url, headers=headers, json={
                "query": f"""
                query ($q: String!) {{
                  products(first: 1, query: $q) {{
                    edges {{ node {{ {MEDIA_FRAGMENT} }} }}
                  }}
                }}
                """,
                "variables": {"q": f"sku:{sku}"},
            })
            edges = (resp2.json().get("data") or {}).get("products", {}).get("edges", [])
            if edges:
                product = edges[0]["node"]

        # 3️⃣ Fallback: search by title keywords
        if not product:
            resp3 = await client.post(url, headers=headers, json={
                "query": f"""
                query ($q: String!) {{
                  products(first: 1, query: $q) {{
                    edges {{ node {{ {MEDIA_FRAGMENT} }} }}
                  }}
                }}
                """,
                "variables": {"q": f"tag:{handle} OR sku:{handle}"},
            })
            edges = (resp3.json().get("data") or {}).get("products", {}).get("edges", [])
            if edges:
                product = edges[0]["node"]

    if not product:
        return JSONResponse(status_code=404, content={"error": f"Prodotto non trovato (handle={handle}, sku={sku})"})

    media = [
        {
            "id": m["id"],
            "url": (m.get("image") or {}).get("url"),
            "alt": m.get("alt"),
            "status": m.get("fileStatus"),
        }
        for m in product["media"]["nodes"]
        if m.get("mediaContentType") == "IMAGE" and (m.get("image") or {}).get("url")
    ]

    return {"id": product["id"], "title": product["title"], "media": media}


# ── SHOPIFY: upload image from URL ───────────────────────────────────────────

@app.post("/api/shopify/media/upload-url")
async def upload_image_from_url(
    shop: str = Form(...),
    product_id: str = Form(...),
    image_url: str = Form(...),
    alt: str = Form(""),
):
    """Add an image to a Shopify product from a remote URL."""
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    mutation = """
    mutation productCreateMedia($productId: ID!, $media: [CreateMediaInput!]!) {
      productCreateMedia(productId: $productId, media: $media) {
        media {
          id
          mediaContentType
          ... on MediaImage {
            fileStatus
            image { url }
          }
        }
        mediaUserErrors { field message }
      }
    }
    """

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            },
            json={
                "query": mutation,
                "variables": {
                    "productId": product_id,
                    "media": [{"originalSource": image_url, "alt": alt, "mediaContentType": "IMAGE"}],
                },
            },
        )

    data = resp.json()
    result = (data.get("data") or {}).get("productCreateMedia", {})
    errors = result.get("mediaUserErrors", [])
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})

    created = result.get("media", [])
    return {"created": created}


# ── SHOPIFY: upload image file (binary) ──────────────────────────────────────

@app.post("/api/shopify/media/upload-file")
async def upload_image_file(
    shop: str = Form(...),
    product_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload a local image file to Shopify via staged upload."""
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    mime = file.content_type or "image/jpeg"
    allowed_mimes = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if mime not in allowed_mimes:
        return JSONResponse(status_code=400, content={"error": f"Unsupported file type: {mime}"})

    binary = await file.read()
    filename = file.filename or "upload.jpg"

    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    base_url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"

    async with httpx.AsyncClient(timeout=120) as client:
        # 1. Create staged upload target
        stage_mutation = """
        mutation CreateStagedUploads($input: [StagedUploadInput!]!) {
          stagedUploadsCreate(input: $input) {
            stagedTargets { url resourceUrl parameters { name value } }
            userErrors { field message }
          }
        }
        """
        stage_resp = await client.post(
            base_url,
            headers=headers,
            json={
                "query": stage_mutation,
                "variables": {
                    "input": [{
                        "filename": filename,
                        "mimeType": mime,
                        "resource": "IMAGE",
                        "fileSize": str(len(binary)),
                        "httpMethod": "PUT",
                    }]
                },
            },
        )
        stage_data = stage_resp.json()
        targets = stage_data["data"]["stagedUploadsCreate"]["stagedTargets"]
        if not targets:
            return JSONResponse(status_code=500, content={"error": "Staged upload creation failed"})

        target = targets[0]
        params = {p["name"]: p["value"] for p in target["parameters"]}

        # 2. PUT binary to staged URL
        put_resp = await client.put(target["url"], content=binary, headers=params)
        if put_resp.status_code not in (200, 201):
            return JSONResponse(status_code=400, content={"error": f"Upload failed: {put_resp.text[:200]}"})

        # 3. Create media from resource URL
        create_mutation = """
        mutation productCreateMedia($productId: ID!, $media: [CreateMediaInput!]!) {
          productCreateMedia(productId: $productId, media: $media) {
            media {
              id
              ... on MediaImage { fileStatus image { url } }
            }
            mediaUserErrors { field message }
          }
        }
        """
        create_resp = await client.post(
            base_url,
            headers=headers,
            json={
                "query": create_mutation,
                "variables": {
                    "productId": product_id,
                    "media": [{"originalSource": target["resourceUrl"], "mediaContentType": "IMAGE", "alt": filename}],
                },
            },
        )
        create_data = create_resp.json()
        result = (create_data.get("data") or {}).get("productCreateMedia", {})
        errors = result.get("mediaUserErrors", [])
        if errors:
            return JSONResponse(status_code=400, content={"errors": errors})

        return {"created": result.get("media", [])}


# ── SHOPIFY: delete media ────────────────────────────────────────────────────

@app.delete("/api/shopify/media")
async def delete_media(shop: str, product_id: str, media_id: str):
    """Delete a media item from a Shopify product."""
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    mutation = """
    mutation DeleteMedia($productId: ID!, $mediaIds: [ID!]!) {
      productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
        deletedMediaIds
        mediaUserErrors { field message }
      }
    }
    """

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={"query": mutation, "variables": {"productId": product_id, "mediaIds": [media_id]}},
        )

    data = resp.json()
    result = (data.get("data") or {}).get("productDeleteMedia", {})
    errors = result.get("mediaUserErrors", [])
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})

    return {"deleted": result.get("deletedMediaIds", [])}