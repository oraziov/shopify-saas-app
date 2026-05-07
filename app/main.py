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

    FIELDS = """
        id title
        images(first:50){edges{node{id url altText}}}
        variants(first:100){
          edges{node{
            id title sku
            image{id url}
            selectedOptions{name value}
          }}
        }
    """

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
        return JSONResponse(status_code=404, content={"error": f"Prodotto non trovato (handle={handle})"})

    images = [
        {"id": e["node"]["id"], "url": e["node"]["url"], "alt": e["node"].get("altText") or ""}
        for e in product["images"]["edges"]
    ]

    # Group variants by color option value
    color_names = {"colore", "color", "colour"}
    variants = []
    for e in product["variants"]["edges"]:
        v = e["node"]
        color_opt = next(
            (o["value"] for o in v.get("selectedOptions", [])
             if o["name"].lower() in color_names or "col" in o["name"].lower()),
            None
        )
        variants.append({
            "id": v["id"],
            "sku": v["sku"],
            "title": v["title"],
            "color": color_opt,
            "image_id": (v.get("image") or {}).get("id"),
            "image_url": (v.get("image") or {}).get("url"),
        })

    return {"id": product["id"], "title": product["title"], "images": images, "variants": variants}


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


@app.post("/api/shopify/variant/assign-color")
async def assign_color_image(
    shop: str = Form(...),
    product_id: str = Form(...),
    image_id: str = Form(...),   # gid://shopify/ProductImage/...
    color: str = Form(...),      # e.g. "NERO"
):
    """Assign a product image to all variants of a given color."""
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    color_names = {"colore", "color", "colour"}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(gql, headers=h, json={
            "query": """query($id:ID!){product(id:$id){variants(first:100){edges{node{
              id selectedOptions{name value}}}}}}""",
            "variables": {"id": product_id},
        })
        edges = (r.json().get("data") or {}).get("product", {}).get("variants", {}).get("edges", [])

        # Auto-detect which option is the color one
        color_opt_name = None
        if edges:
            for opt in edges[0]["node"].get("selectedOptions", []):
                n = opt["name"].lower()
                if n in color_names or "col" in n:
                    color_opt_name = opt["name"]
                    break

        all_options = []
        target_ids = []
        for e in edges:
            opts = e["node"].get("selectedOptions", [])
            all_options.extend([(o["name"], o["value"]) for o in opts])
            for o in opts:
                n = o["name"].lower()
                is_color = (color_opt_name and o["name"] == color_opt_name) \
                           or n in color_names or "col" in n
                if is_color and (o["value"] == color or o["value"].upper() == color.upper()):
                    target_ids.append(e["node"]["id"])
                    break

        logger.info(f"assign_color: color={color}, detected_opt={color_opt_name}, found={len(target_ids)}")

        if not target_ids:
            return JSONResponse(status_code=404, content={
                "error": f"Nessuna variante trovata con colore '{color}'",
                "debug_options": list(set(all_options))[:30],
            })

        # Bulk assign
        resp = await client.post(gql, headers=h, json={
            "query": """mutation($productId:ID!,$variants:[ProductVariantsBulkInput!]!){
              productVariantsBulkUpdate(productId:$productId,variants:$variants){
                productVariants{id image{id url}}
                userErrors{field message}
              }}""",
            "variables": {
                "productId": product_id,
                "variants": [{"id": vid, "imageId": image_id} for vid in target_ids],
            },
        })

    result = (resp.json().get("data") or {}).get("productVariantsBulkUpdate", {})
    errors = result.get("userErrors", [])
    if errors:
        return JSONResponse(status_code=400, content={"errors": errors})

    return {"ok": True, "updated": len(result.get("productVariants", [])), "color": color}


# ── SHOPIFY: list files (Content > Files) ────────────────────────────────────

@app.get("/api/shopify/files")
async def list_files(shop: str, after: str = "", q: str = ""):
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    gql = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    variables: dict = {"first": 48}
    if after:
        variables["after"] = after
    if q:
        variables["query"] = f"filename:*{q}*"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(gql, headers=h, json={
            "query": """
            query($first:Int!, $after:String, $query:String) {
              files(first:$first, after:$after, query:$query,
                    sortKey:CREATED_AT, reverse:true) {
                pageInfo { hasNextPage endCursor }
                edges {
                  node {
                    __typename
                    ... on MediaImage {
                      id
                      alt
                      image { url }
                    }
                  }
                }
              }
            }""",
            "variables": variables,
        })

    data = resp.json()
    if "errors" in data:
        return JSONResponse(status_code=400, content={"error": str(data["errors"])})

    files_data = (data.get("data") or {}).get("files", {})
    page_info = files_data.get("pageInfo", {})

    files = []
    for edge in files_data.get("edges", []):
        node = edge["node"]
        if node.get("__typename") != "MediaImage":
            continue
        url = (node.get("image") or {}).get("url")
        if not url:
            continue
        files.append({
            "id": node["id"],
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


# ── DEBUG: mostra option names reali delle varianti ───────────────────────────

@app.get("/api/shopify/product/options")
async def get_product_options(shop: str, product_id: str):
    """Restituisce i nomi delle option e i valori unici per ogni option."""
    token = get_shop_token(shop)
    if not token:
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://{shop}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={
                "query": """query($id:ID!){product(id:$id){
                  options{name values}
                  variants(first:5){edges{node{selectedOptions{name value}}}}
                }}""",
                "variables": {"id": product_id},
            },
        )
    data = (resp.json().get("data") or {}).get("product", {})
    return {
        "options": data.get("options", []),
        "sample_variants": [
            e["node"]["selectedOptions"]
            for e in data.get("variants", {}).get("edges", [])
        ],
    }