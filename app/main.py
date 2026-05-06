import asyncio
import csv
import io
import json
import logging
import time

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
        color_code = str(row.get("color_code") or "").strip()
        colore = str(row.get("Colore") or row.get("colore") or "").strip()
        sku = str(row.get("SKU") or row.get("Variant SKU") or "").strip()
        taglia = str(row.get("Taglia") or row.get("taglia") or "").strip()
        key = f"{handle}__{color_code}"
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


# ── SHOPIFY: fetch product + variants + gallery ──────────────────────────────

PRODUCT_FIELDS = """
    id title
    media(first:50){
      nodes{
        id mediaContentType
        ... on MediaImage{ fileStatus image{url} alt }
      }
    }
    variants(first:100){
      edges{
        node{
          id sku title
          image{ id url }
          selectedOptions{name value}
          metafield(namespace:"custom",key:"gallery"){
            id value
            references(first:20){
              nodes{
                ... on MediaImage{ id image{url} alt fileStatus }
              }
            }
          }
        }
      }
    }
"""


@app.get("/api/shopify/product")
async def get_shopify_product(shop: str, handle: str, sku: str = ""):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Shop not authenticated"})

    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(gql, headers=h, json={
            "query": f"query($h:String!){{productByHandle(handle:$h){{{PRODUCT_FIELDS}}}}}",
            "variables": {"h": handle},
        })
        product = (r.json().get("data") or {}).get("productByHandle")

        if not product and sku:
            r2 = await client.post(gql, headers=h, json={
                "query": f"query($q:String!){{products(first:1,query:$q){{edges{{node{{{PRODUCT_FIELDS}}}}}}}}}",
                "variables": {"q": f"sku:{sku}"},
            })
            edges = (r2.json().get("data") or {}).get("products", {}).get("edges", [])
            if edges:
                product = edges[0]["node"]

        if not product:
            r3 = await client.post(gql, headers=h, json={
                "query": f"query($q:String!){{products(first:1,query:$q){{edges{{node{{{PRODUCT_FIELDS}}}}}}}}}",
                "variables": {"q": f"sku:{handle}"},
            })
            edges = (r3.json().get("data") or {}).get("products", {}).get("edges", [])
            if edges:
                product = edges[0]["node"]

    if not product:
        return JSONResponse(status_code=404, content={"error": f"Prodotto non trovato (handle={handle}, sku={sku})"})

    media = [
        {"id": m["id"], "url": (m.get("image") or {}).get("url"), "alt": m.get("alt") or "", "status": m.get("fileStatus")}
        for m in product["media"]["nodes"]
        if m.get("mediaContentType") == "IMAGE" and (m.get("image") or {}).get("url")
    ]

    variants = []
    for edge in product["variants"]["edges"]:
        v = edge["node"]
        mf = v.get("metafield") or {}
        gallery = [
            {"id": n["id"], "url": (n.get("image") or {}).get("url"), "alt": n.get("alt") or ""}
            for n in (mf.get("references") or {}).get("nodes", [])
            if (n.get("image") or {}).get("url")
        ]
        variants.append({
            "id": v["id"], "sku": v["sku"], "title": v["title"],
            "options": v.get("selectedOptions", []),
            "image": v.get("image"),
            "metafield_id": mf.get("id"),
            "gallery": gallery,
        })

    return {"id": product["id"], "title": product["title"], "media": media, "variants": variants}


# ── SHOPIFY: upload from URL ─────────────────────────────────────────────────

@app.post("/api/shopify/media/upload-url")
async def upload_image_from_url(shop: str = Form(...), product_id: str = Form(...), image_url: str = Form(...), alt: str = Form("")):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={
                "query": """mutation($productId:ID!,$media:[CreateMediaInput!]!){
                  productCreateMedia(productId:$productId,media:$media){
                    media{id mediaContentType ... on MediaImage{fileStatus image{url}}}
                    mediaUserErrors{field message}
                  }}""",
                "variables": {"productId": product_id, "media": [{"originalSource": image_url, "alt": alt, "mediaContentType": "IMAGE"}]},
            },
        )
    result = (resp.json().get("data") or {}).get("productCreateMedia", {})
    errors = result.get("mediaUserErrors", [])
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    media = result.get("media", [])
    if not media:
        return JSONResponse(status_code=500, content={"error": "Nessun media restituito"})
    m = media[0]
    return {"id": m["id"], "url": (m.get("image") or {}).get("url"), "status": m.get("fileStatus")}


# ── SHOPIFY: upload file binary ──────────────────────────────────────────────

@app.post("/api/shopify/media/upload-file")
async def upload_image_file(shop: str = Form(...), product_id: str = Form(...), file: UploadFile = File(...)):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    mime = file.content_type or "image/jpeg"
    if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        return JSONResponse(status_code=400, content={"error": f"Tipo non supportato: {mime}"})
    binary = await file.read()
    filename = file.filename or "upload.jpg"
    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            stage = await client.post(gql, headers=h, json={
                "query": """mutation($input:[StagedUploadInput!]!){stagedUploadsCreate(input:$input){
                  stagedTargets{url resourceUrl parameters{name value}} userErrors{field message}}}""",
                "variables": {"input": [{"filename": filename, "mimeType": mime, "resource": "IMAGE", "fileSize": str(len(binary)), "httpMethod": "PUT"}]},
            })
            targets = stage.json()["data"]["stagedUploadsCreate"]["stagedTargets"]
            if not targets:
                return JSONResponse(status_code=500, content={"error": "Staged upload fallito"})
            target = targets[0]
            put_h = {p["name"]: p["value"] for p in target["parameters"]}
            put = await client.put(target["url"], content=binary, headers=put_h)
            if put.status_code not in (200, 201):
                return JSONResponse(status_code=400, content={"error": f"Upload fallito: {put.text[:200]}"})
            resp = await client.post(gql, headers=h, json={
                "query": """mutation($productId:ID!,$media:[CreateMediaInput!]!){
                  productCreateMedia(productId:$productId,media:$media){
                    media{id ... on MediaImage{fileStatus image{url}}}
                    mediaUserErrors{field message}}}""",
                "variables": {"productId": product_id, "media": [{"originalSource": target["resourceUrl"], "mediaContentType": "IMAGE", "alt": filename}]},
            })
            result = (resp.json().get("data") or {}).get("productCreateMedia", {})
            errors = result.get("mediaUserErrors", [])
            if errors:
                return JSONResponse(status_code=400, content={"errors": errors})
            media = result.get("media", [])
            if not media:
                return JSONResponse(status_code=500, content={"error": "Nessun media restituito"})
            m = media[0]
            return {"id": m["id"], "url": (m.get("image") or {}).get("url"), "status": m.get("fileStatus")}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── SHOPIFY: delete product media ────────────────────────────────────────────

@app.delete("/api/shopify/media")
async def delete_media(shop: str, product_id: str, media_id: str):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={
                "query": """mutation($productId:ID!,$mediaIds:[ID!]!){
                  productDeleteMedia(productId:$productId,mediaIds:$mediaIds){
                    deletedMediaIds mediaUserErrors{field message}}}""",
                "variables": {"productId": product_id, "mediaIds": [media_id]},
            },
        )
    result = (resp.json().get("data") or {}).get("productDeleteMedia", {})
    errors = result.get("mediaUserErrors", [])
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    return {"deleted": result.get("deletedMediaIds", [])}


# ── SHOPIFY: variant gallery metafield ───────────────────────────────────────

async def _get_gallery_ids(client, gql, h, variant_id) -> list:
    r = await client.post(gql, headers=h, json={
        "query": """query($id:ID!){node(id:$id){... on ProductVariant{
          metafield(namespace:"custom",key:"gallery"){value}}}}""",
        "variables": {"id": variant_id},
    })
    mf = ((r.json().get("data") or {}).get("node") or {}).get("metafield") or {}
    return json.loads(mf.get("value") or "[]")


async def _set_gallery_ids(client, gql, h, variant_id, ids: list):
    return await client.post(gql, headers=h, json={
        "query": """mutation($mf:[MetafieldsSetInput!]!){metafieldsSet(metafields:$mf){
          userErrors{field message}}}""",
        "variables": {"mf": [{"ownerId": variant_id, "namespace": "custom", "key": "gallery",
                               "type": "list.file_reference", "value": json.dumps(ids)}]},
    })


@app.post("/api/shopify/variant/gallery/add")
async def add_to_gallery(shop: str = Form(...), variant_id: str = Form(...), media_id: str = Form(...)):
    """Append a media GID to the variant gallery metafield."""
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        ids = await _get_gallery_ids(client, gql, h, variant_id)
        if media_id not in ids:
            ids.append(media_id)
        resp = await _set_gallery_ids(client, gql, h, variant_id, ids)
    errors = (resp.json().get("data") or {}).get("metafieldsSet", {}).get("userErrors", [])
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    return {"ok": True, "gallery": ids}


@app.delete("/api/shopify/variant/gallery/item")
async def remove_from_gallery(shop: str, variant_id: str, media_id: str):
    """Remove a media GID from the variant gallery metafield."""
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        ids = await _get_gallery_ids(client, gql, h, variant_id)
        ids = [i for i in ids if i != media_id]
        resp = await _set_gallery_ids(client, gql, h, variant_id, ids)
    errors = (resp.json().get("data") or {}).get("metafieldsSet", {}).get("userErrors", [])
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    return {"ok": True, "gallery": ids}


@app.post("/api/shopify/variant/gallery/sync")
async def sync_gallery_to_color(
    shop: str = Form(...),
    variant_ids: str = Form(...),   # JSON array of all variant GIDs with same color
    media_ids: str = Form(...),     # JSON array of media GIDs to set as gallery
):
    """Set the same gallery on all variants of the same color at once."""
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    try:
        vids = json.loads(variant_ids)
        mids = json.loads(media_ids)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "JSON non valido"})

    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        # Shopify metafieldsSet supports up to 25 at once
        metafields = [
            {"ownerId": vid, "namespace": "custom", "key": "gallery",
             "type": "list.file_reference", "value": json.dumps(mids)}
            for vid in vids
        ]
        resp = await client.post(gql, headers=h, json={
            "query": """mutation($mf:[MetafieldsSetInput!]!){metafieldsSet(metafields:$mf){
              userErrors{field message}}}""",
            "variables": {"mf": metafields},
        })

    errors = (resp.json().get("data") or {}).get("metafieldsSet", {}).get("userErrors", [])
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})
    return {"ok": True, "updated_variants": len(vids), "gallery_count": len(mids)}


# ── SHOPIFY: assegna immagini alle varianti colore (1 img/variante) ──────────

@app.post("/api/shopify/variant/assign-images")
async def assign_images_to_variants(
    shop: str = Form(...),
    variant_ids: str = Form(...),   # JSON array, ordinato per taglia
    media_ids: str = Form(...),     # JSON array di image GID da distribuire
):
    """
    Distribuisce le immagini sulle varianti dello stesso colore.
    Shopify limita a 1 immagine per variante, ma assegnando immagini diverse
    a varianti diverse dello stesso colore, il tema le mostra tutte insieme.

    Strategia:
    - Se immagini >= varianti: ogni variante prende 1 immagine diversa
    - Se immagini < varianti: le immagini vengono distribuite ciclicamente
    - Tutte le immagini vengono anche aggiunte al prodotto se non già presenti
    """
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    try:
        vids = json.loads(variant_ids)
        mids = json.loads(media_ids)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "JSON non valido"})

    if not vids or not mids:
        return JSONResponse(status_code=400, content={"error": "variant_ids e media_ids non possono essere vuoti"})

    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    # Shopify REST per assegnare image_id alla variante
    # GraphQL productVariantUpdate accetta imageId
    results = []
    errors_list = []

    async with httpx.AsyncClient(timeout=60) as client:
        for i, vid in enumerate(vids):
            image_gid = mids[i % len(mids)]   # distribuzione ciclica

            resp = await client.post(gql, headers=h, json={
                "query": """
                mutation($input: ProductVariantInput!) {
                  productVariantUpdate(input: $input) {
                    productVariant { id image { url } }
                    userErrors { field message }
                  }
                }""",
                "variables": {
                    "input": {
                        "id": vid,
                        "imageId": image_gid,
                    }
                },
            })

            result = (resp.json().get("data") or {}).get("productVariantUpdate", {})
            errs = result.get("userErrors", [])
            if errs:
                errors_list.append({"variant": vid, "errors": errs})
            else:
                variant_data = result.get("productVariant", {})
                results.append({
                    "variant_id": vid,
                    "image_url": (variant_data.get("image") or {}).get("url"),
                    "image_gid": image_gid,
                })

    if errors_list and not results:
        return JSONResponse(status_code=400, content={"errors": errors_list})

    return {
        "ok": True,
        "assigned": len(results),
        "errors": errors_list,
        "results": results,
    }