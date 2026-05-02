from fastapi import FastAPI, Request, HTTPException, UploadFile, Form, File, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from urllib.parse import urlencode
import requests
import mimetypes
import time
import json
import csv

from fastapi.templating import Jinja2Templates

from app.config import SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, APP_URL
from app.db import init_db, save_shop_token, get_shop_token, get_conn

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup():
    init_db()


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# 🔹 INSTALL
@app.get("/install")
def install(shop: str):
    url = f"https://{shop}/admin/oauth/authorize?" + urlencode({
        "client_id": SHOPIFY_CLIENT_ID,
        "scope": "read_products,write_products",
        "redirect_uri": f"{APP_URL}/callback",
    })
    return RedirectResponse(url)


# 🔹 CALLBACK
@app.get("/callback")
def callback(request: Request):
    shop = request.query_params.get("shop")
    code = request.query_params.get("code")

    res = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
            "code": code,
        },
    ).json()

    token = res.get("access_token")

    if not token:
        raise HTTPException(400, "No token")

    save_shop_token(shop, token)

    return {"ok": True}


# 🔥 PRODUCTS
@app.get("/products")
def get_products(shop: str, query: str = ""):
    token = get_shop_token(shop)

    gql = """
    query ($query: String!) {
      products(first: 50, sortKey: CREATED_AT, reverse: true, query: $query) {
        edges {
          node {
            id
            title
            media(first: 20) {
              nodes {
                id
                ... on MediaImage {
                  image { url }
                }
              }
            }
            variants(first: 50) {
              nodes {
                id
                title
              }
            }
          }
        }
      }
    }
    """

    res = requests.post(
        f"https://{shop}/admin/api/2026-04/graphql.json",
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        },
        json={"query": gql, "variables": {"query": query}}
    ).json()

    out = []

    for edge in res["data"]["products"]["edges"]:
        p = edge["node"]

        images = [
            {"id": m["id"], "url": m["image"]["url"]}
            for m in p["media"]["nodes"]
            if m.get("image")
        ]

        variants = [
            {"id": v["id"], "title": v["title"]}
            for v in p["variants"]["nodes"]
        ]

        out.append({
            "id": p["id"],
            "title": p["title"],
            "images": images,
            "variants": variants
        })

    return out


# 🔥 SET VARIANT IMAGE (FIX CRITICO)
@app.post("/variant/image/set")
def set_variant_image(
    shop: str = Form(...),
    variant_id: str = Form(...),
    image_id: str = Form(...)
):
    token = get_shop_token(shop)

    variant_numeric = variant_id.split("/")[-1]
    image_numeric = image_id.split("/")[-1]

    res = requests.put(
        f"https://{shop}/admin/api/2026-04/variants/{variant_numeric}.json",
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        },
        json={
            "variant": {
                "id": int(variant_numeric),
                "image_id": int(image_numeric)
            }
        }
    )

    return res.json()


# 🔥 CSV IMPORT (DB ONLY)
@app.post("/csv/upload")
async def upload_csv(shop: str = Form(...), file: UploadFile = File(...)):
    content = (await file.read()).decode("utf-8-sig").splitlines()
    reader = csv.DictReader(content)

    rows = list(reader)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM csv_catalog WHERE shop = %s", (shop,))

            for r in rows:
                cur.execute("""
                INSERT INTO csv_catalog
                (shop, handle, brand, title, sku, colore, taglia, color_code, image1, image2, image3, raw)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    shop,
                    r.get("handle"),
                    r.get("Brand"),
                    r.get("Title"),
                    r.get("SKU"),
                    r.get("Colore"),
                    r.get("Taglia"),
                    r.get("color_code"),
                    r.get("Image1"),
                    r.get("Image2"),
                    r.get("Image3"),
                    json.dumps(r),
                ))

    return {"ok": True, "rows": len(rows)}