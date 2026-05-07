import csv
import io
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

        if handle not in products:
            products[handle] = {
                "handle": handle,
                "brand": str(row.get("Brand") or "").strip(),
                "title": str(row.get("Title") or "").strip(),
                "colors": [],
            }

        # Add color if not already present
        existing = [c["color_code"] for c in products[handle]["colors"]]
        if color_code not in existing:
            products[handle]["colors"].append({
                "colore": colore,
                "color_code": color_code,
            })

    return list(products.values())


@app.get("/api/shopify/product")
async def get_shopify_product(shop: str, handle: str):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Shop non autenticato"})

    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    FIELDS = "id title images(first:50){edges{node{id url altText}}}"

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(gql, headers=h, json={
            "query": f"query($h:String!){{productByHandle(handle:$h){{{FIELDS}}}}}",
            "variables": {"h": handle},
        })
        product = (r.json().get("data") or {}).get("productByHandle")

        if not product:
            r2 = await client.post(gql, headers=h, json={
                "query": f"query($q:String!){{products(first:5,query:$q){{edges{{node{{{FIELDS}}}}}}}}}",
                "variables": {"q": handle},
            })
            edges = (r2.json().get("data") or {}).get("products", {}).get("edges", [])
            if edges:
                product = edges[0]["node"]

    if not product:
        return JSONResponse(status_code=404, content={"error": f"Prodotto non trovato per handle={handle}"})

    images = [
        {"id": e["node"]["id"], "url": e["node"]["url"], "alt": e["node"].get("altText") or ""}
        for e in product["images"]["edges"]
    ]
    return {"id": product["id"], "title": product["title"], "images": images}


@app.post("/api/shopify/image/upload-url")
async def upload_from_url(shop: str = Form(...), product_id: str = Form(...), image_url: str = Form(...), alt: str = Form("")):
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
        return JSONResponse(status_code=400, content={"error": str(data.get("errors", "Upload fallito"))})
    img = data["image"]
    return {"id": f"gid://shopify/ProductImage/{img['id']}", "url": img["src"], "alt": img.get("alt") or ""}


@app.post("/api/shopify/image/upload-file")
async def upload_file(shop: str = Form(...), product_id: str = Form(...), file: UploadFile = File(...)):
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
    async with httpx.AsyncClient(timeout=120) as client:
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
        put = await client.put(target["url"], content=binary, headers={p["name"]: p["value"] for p in target["parameters"]})
        if put.status_code not in (200, 201):
            return JSONResponse(status_code=400, content={"error": f"Upload fallito: {put.text[:200]}"})
        attach = await client.post(
            f"https://{shop}/admin/api/{API_VERSION}/products/{rest_id}/images.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={"image": {"src": target["resourceUrl"], "alt": filename}},
        )
    data = attach.json()
    if "image" not in data:
        return JSONResponse(status_code=400, content={"error": str(data.get("errors", "Attach fallito"))})
    img = data["image"]
    return {"id": f"gid://shopify/ProductImage/{img['id']}", "url": img["src"], "alt": img.get("alt") or ""}


@app.delete("/api/shopify/image")
async def delete_image(shop: str, product_id: str, image_id: str):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"https://{shop}/admin/api/{API_VERSION}/products/{product_id.split('/')[-1]}/images/{image_id.split('/')[-1]}.json",
            headers={"X-Shopify-Access-Token": token},
        )
    if resp.status_code not in (200, 204):
        return JSONResponse(status_code=400, content={"error": f"Errore: {resp.text[:200]}"})
    return {"deleted": image_id}


# ── SHOPIFY: list files (Content > Files) ────────────────────────────────────

@app.get("/api/shopify/files")
async def list_files(shop: str, after: str = "", q: str = ""):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    after_clause = f', after: "{after}"' if after else ""
    query_clause = f', query: "filename:*{q}*"' if q else ""

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(gql, headers=h, json={
            "query": f"""query {{
              files(first: 48{after_clause}{query_clause}, sortKey: CREATED_AT, reverse: true) {{
                pageInfo {{ hasNextPage endCursor }}
                edges {{
                  node {{
                    ... on MediaImage {{
                      id alt
                      image {{ url }}
                      originalFileSize
                    }}
                    ... on GenericFile {{
                      id alt url
                    }}
                  }}
                }}
              }}
            }}""",
        })

    data = resp.json()
    files_data = (data.get("data") or {}).get("files", {})
    page_info = files_data.get("pageInfo", {})

    files = []
    for edge in files_data.get("edges", []):
        node = edge["node"]
        url = (node.get("image") or {}).get("url") or node.get("url")
        if url and ("image" in (node.get("__typename", "")) or url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))):
            files.append({
                "id": node.get("id"),
                "url": url,
                "alt": node.get("alt") or "",
                "filename": url.split("/")[-1].split("?")[0],
            })

    return {
        "files": files,
        "next_cursor": page_info.get("endCursor") if page_info.get("hasNextPage") else None,
    }


# ── SHOPIFY: search products ──────────────────────────────────────────────────

@app.get("/api/shopify/products/search")
async def search_products(shop: str, q: str):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={
                "query": """query($q:String!){products(first:20,query:$q){edges{node{
                  id title handle
                  images(first:1){edges{node{url}}}
                  media(first:1){nodes{id}}
                }}}}""",
                "variables": {"q": q},
            },
        )

    edges = (resp.json().get("data") or {}).get("products", {}).get("edges", [])
    return [
        {
            "id": e["node"]["id"],
            "title": e["node"]["title"],
            "handle": e["node"]["handle"],
            "image_count": len(e["node"]["images"]["edges"]),
            "thumb": (e["node"]["images"]["edges"][0]["node"]["url"] if e["node"]["images"]["edges"] else None),
        }
        for e in edges
    ]