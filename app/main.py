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

logger = logging.getLogger(__name__)
app = FastAPI(title="Shopify Image Manager")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def ui(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── CSV PARSE ────────────────────────────────────────────────────────────────

@app.post("/api/csv/parse")
async def parse_csv(file: UploadFile = File(...)):
    contents = await file.read()
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        text = contents.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    products: dict[str, dict] = {}

    for row in reader:
        handle = str(row.get("handle") or "").strip()
        if not handle:
            continue
        colore = str(row.get("Colore") or row.get("colore") or "").strip()
        color_code = str(row.get("color_code") or "").strip()
        sku = str(row.get("SKU") or row.get("Variant SKU") or "").strip()
        taglia = str(row.get("Taglia") or row.get("taglia") or "").strip()
        key = f"{handle}__{colore}"
        if key not in products:
            products[key] = {
                "handle": handle,
                "brand": str(row.get("Brand") or "").strip(),
                "title": str(row.get("Title") or "").strip(),
                "colore": colore,
                "color_code": color_code,
                "variants": [],
            }
        if sku:
            products[key]["variants"].append({"sku": sku, "taglia": taglia})

    return list(products.values())


# ── SHOPIFY: fetch product media + variants ──────────────────────────────────

@app.get("/api/shopify/product")
async def get_shopify_product(shop: str, handle: str, sku: str = ""):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Shop non autenticato"})

    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    FIELDS = """
        id title
        images(first: 50) {
          edges { node { id url altText } }
        }
        variants(first: 100) {
          edges {
            node {
              id sku title
              image { id url }
              selectedOptions { name value }
            }
          }
        }
    """

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Try by handle slug
        r = await client.post(gql, headers=h, json={
            "query": f"query($h:String!){{productByHandle(handle:$h){{{FIELDS}}}}}",
            "variables": {"h": handle},
        })
        product = (r.json().get("data") or {}).get("productByHandle")

        # 2. Fallback: search by SKU
        if not product and sku:
            r2 = await client.post(gql, headers=h, json={
                "query": f"query($q:String!){{products(first:1,query:$q){{edges{{node{{{FIELDS}}}}}}}}}",
                "variables": {"q": f"sku:{sku}"},
            })
            edges = (r2.json().get("data") or {}).get("products", {}).get("edges", [])
            if edges:
                product = edges[0]["node"]

    if not product:
        return JSONResponse(status_code=404, content={"error": f"Prodotto non trovato (handle={handle})"})

    images = [
        {"id": e["node"]["id"], "url": e["node"]["url"], "alt": e["node"].get("altText") or ""}
        for e in product["images"]["edges"]
    ]

    variants = [
        {
            "id": e["node"]["id"],
            "sku": e["node"]["sku"],
            "title": e["node"]["title"],
            "image_id": (e["node"].get("image") or {}).get("id"),
            "image_url": (e["node"].get("image") or {}).get("url"),
            "options": e["node"].get("selectedOptions", []),
        }
        for e in product["variants"]["edges"]
    ]

    return {"id": product["id"], "title": product["title"], "images": images, "variants": variants}


# ── SHOPIFY: upload image from URL ───────────────────────────────────────────

@app.post("/api/shopify/image/upload-url")
async def upload_image_from_url(
    shop: str = Form(...),
    product_id: str = Form(...),
    image_url: str = Form(...),
    alt: str = Form(""),
):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    rest_id = product_id.split("/")[-1]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://{shop}/admin/api/{API_VERSION}/products/{rest_id}/images.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={"image": {"src": image_url, "alt": alt}},
        )

    data = resp.json()
    if "image" not in data:
        return JSONResponse(status_code=400, content={"error": data.get("errors", "Upload fallito")})

    img = data["image"]
    return {"id": f"gid://shopify/ProductImage/{img['id']}", "url": img["src"], "alt": img.get("alt") or ""}


# ── SHOPIFY: upload image file ────────────────────────────────────────────────

@app.post("/api/shopify/image/upload-file")
async def upload_image_file(
    shop: str = Form(...),
    product_id: str = Form(...),
    file: UploadFile = File(...),
):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    mime = file.content_type or "image/jpeg"
    if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        return JSONResponse(status_code=400, content={"error": f"Tipo non supportato: {mime}"})

    binary = await file.read()
    filename = file.filename or "upload.jpg"
    rest_id = product_id.split("/")[-1]
    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            # Staged upload
            stage = await client.post(gql, headers=h, json={
                "query": """mutation($input:[StagedUploadInput!]!){stagedUploadsCreate(input:$input){
                  stagedTargets{url resourceUrl parameters{name value}}userErrors{message}}}""",
                "variables": {"input": [{"filename": filename, "mimeType": mime,
                    "resource": "IMAGE", "fileSize": str(len(binary)), "httpMethod": "PUT"}]},
            })
            targets = stage.json()["data"]["stagedUploadsCreate"]["stagedTargets"]
            if not targets:
                return JSONResponse(status_code=500, content={"error": "Staged upload fallito"})
            target = targets[0]
            put_h = {p["name"]: p["value"] for p in target["parameters"]}
            put = await client.put(target["url"], content=binary, headers=put_h)
            if put.status_code not in (200, 201):
                return JSONResponse(status_code=400, content={"error": f"Upload fallito: {put.text[:200]}"})

            # Attach to product via REST (returns ProductImage with proper ID)
            attach = await client.post(
                f"https://{shop}/admin/api/{API_VERSION}/products/{rest_id}/images.json",
                headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
                json={"image": {"src": target["resourceUrl"], "alt": filename}},
            )
            img_data = attach.json()
            if "image" not in img_data:
                return JSONResponse(status_code=400, content={"error": img_data.get("errors", "Attach fallito")})

            img = img_data["image"]
            return {"id": f"gid://shopify/ProductImage/{img['id']}", "url": img["src"], "alt": img.get("alt") or ""}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── SHOPIFY: delete image ─────────────────────────────────────────────────────

@app.delete("/api/shopify/image")
async def delete_image(shop: str, product_id: str, image_id: str):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    rest_product = product_id.split("/")[-1]
    rest_image = image_id.split("/")[-1]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"https://{shop}/admin/api/{API_VERSION}/products/{rest_product}/images/{rest_image}.json",
            headers={"X-Shopify-Access-Token": token},
        )
    if resp.status_code not in (200, 204):
        return JSONResponse(status_code=400, content={"error": f"Errore eliminazione: {resp.text[:200]}"})
    return {"deleted": image_id}


# ── SHOPIFY: assign image to color variant ────────────────────────────────────

@app.post("/api/shopify/variant/assign-color-image")
async def assign_color_image(
    shop: str = Form(...),
    product_id: str = Form(...),
    image_id: str = Form(...),    # ProductImage GID  gid://shopify/ProductImage/...
    colore: str = Form(...),      # e.g. "NERO" — matches option value on Shopify
):
    """
    Assigns a product image to ALL variants whose 'Colore'/'Color' option
    matches the given colore value. Uses productVariantsBulkUpdate for efficiency.
    """
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        # Fetch all variants for this product
        r = await client.post(gql, headers=h, json={
            "query": """query($id:ID!){product(id:$id){variants(first:100){edges{node{
              id selectedOptions{name value}}}}}}""",
            "variables": {"id": product_id},
        })
        edges = (r.json().get("data") or {}).get("product", {}).get("variants", {}).get("edges", [])

        # Filter variants by color option
        color_option_names = {"colore", "color", "colour"}
        target_variants = []
        for e in edges:
            opts = e["node"].get("selectedOptions", [])
            for opt in opts:
                if opt["name"].lower() in color_option_names and opt["value"].upper() == colore.upper():
                    target_variants.append(e["node"]["id"])
                    break

        if not target_variants:
            return JSONResponse(status_code=404, content={
                "error": f"Nessuna variante trovata con Colore='{colore}'. "
                         f"Opzioni disponibili: {[o['value'] for e in edges for o in e['node'].get('selectedOptions',[]) if o['name'].lower() in color_option_names]}"
            })

        # Bulk update: assign same image to all color variants
        resp = await client.post(gql, headers=h, json={
            "query": """
            mutation($productId:ID!, $variants:[ProductVariantsBulkInput!]!) {
              productVariantsBulkUpdate(productId:$productId, variants:$variants) {
                productVariants { id image { id url } }
                userErrors { field message }
              }
            }""",
            "variables": {
                "productId": product_id,
                "variants": [{"id": vid, "imageId": image_id} for vid in target_variants],
            },
        })

    result = (resp.json().get("data") or {}).get("productVariantsBulkUpdate", {})
    errors = result.get("userErrors", [])
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})

    updated = result.get("productVariants", [])
    return {
        "ok": True,
        "updated": len(updated),
        "colore": colore,
        "image_id": image_id,
        "variants": [{"id": v["id"], "image_url": (v.get("image") or {}).get("url")} for v in updated],
    }