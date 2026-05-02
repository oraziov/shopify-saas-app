from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import requests, json, csv, io

from app.db import init_db, get_shop_token

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
def startup():
    init_db()

# UI
@app.get("/", response_class=HTMLResponse)
def ui(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# 🔥 PROCESS CSV
@app.post("/csv/process")
async def process_csv(shop: str = Form(...), file: UploadFile = File(...)):
    token = get_shop_token(shop)

    contents = await file.read()
    reader = csv.DictReader(io.StringIO(contents.decode("utf-8")))

    results = []

    for row in reader:
        handle = row.get("handle")
        sku = row.get("SKU")
        color_code = row.get("color_code")

        query = f"""
        {{
          products(first:1, query:"handle:{handle}") {{
            edges {{
              node {{
                id
                title
                media(first:20) {{
                  nodes {{
                    ... on MediaImage {{
                      id
                      image {{ url }}
                    }}
                  }}
                }}
                variants(first:50) {{
                  edges {{
                    node {{
                      id
                      sku
                      title
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """

        res = requests.post(
            f"https://{shop}/admin/api/2026-04/graphql.json",
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json"
            },
            json={"query": query}
        ).json()

        edges = res.get("data", {}).get("products", {}).get("edges", [])
        if not edges:
            continue

        product = edges[0]["node"]

        variant = None
        for v in product["variants"]["edges"]:
            if v["node"]["sku"] == sku:
                variant = v["node"]

        results.append({
            "product_id": product["id"],
            "title": product["title"],
            "variant_id": variant["id"] if variant else None,
            "variant_title": variant["title"] if variant else "",
            "color_code": color_code,
            "images_shopify": [
                {"id": m["id"], "url": m["image"]["url"]}
                for m in product["media"]["nodes"]
            ],
            "images_csv": [
                row.get("Image1"),
                row.get("Image2"),
                row.get("Image3")
            ]
        })

    return results


# 🔥 IMPORT IMMAGINI SELEZIONATE
@app.post("/images/assign")
def assign_images(
    shop: str = Form(...),
    product_id: str = Form(...),
    variant_id: str = Form(...),
    images: str = Form(...)
):
    token = get_shop_token(shop)
    images = json.loads(images)

    uploaded_ids = []

    for url in images:
        res = requests.post(
            f"https://{shop}/admin/api/2026-04/products/{product_id.split('/')[-1]}/images.json",
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json"
            },
            json={"image": {"src": url}}
        ).json()

        if res.get("image"):
            uploaded_ids.append(res["image"]["id"])

    # assegna prima immagine alla variante
    if uploaded_ids and variant_id:
        requests.put(
            f"https://{shop}/admin/api/2026-04/variants/{variant_id.split('/')[-1]}.json",
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json"
            },
            json={
                "variant": {
                    "id": variant_id.split("/")[-1],
                    "image_id": uploaded_ids[0]
                }
            }
        )

    return {"status": "ok"}