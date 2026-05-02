from fastapi import FastAPI, UploadFile, File, Form, Request
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


# 🔥 CSV PROCESS (CON DEBUG)
@app.post("/csv/process")
async def process_csv(shop: str = Form(...), file: UploadFile = File(...)):
    print("🔥 CSV RECEIVED")

    token = get_shop_token(shop)

    contents = await file.read()
    text = contents.decode("utf-8")

    print("📄 CSV preview:", text[:300])

    reader = csv.DictReader(io.StringIO(text))

    results = []

    for row in reader:
        print("➡️ ROW:", row)

        # 🔥 compatibilità colonne
        handle = row.get("handle") or row.get("Handle")
        sku = row.get("SKU") or row.get("Variant SKU")
        color_code = row.get("color_code") or row.get("Color Code")

        images_csv = [
            row.get("Image1"),
            row.get("Image2"),
            row.get("Image3"),
            row.get("Image Src")
        ]

        if not handle:
            continue

        # 🔍 Shopify query
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
            print("❌ Product not found:", handle)
            continue

        product = edges[0]["node"]

        # 🔍 trova variante
        variant = None
        for v in product["variants"]["edges"]:
            if v["node"]["sku"] == sku:
                variant = v["node"]

        results.append({
            "product_id": product["id"],
            "title": product["title"],
            "variant_id": variant["id"] if variant else "",
            "variant_title": variant["title"] if variant else "",
            "color_code": color_code,
            "images_shopify": [
                {"id": m["id"], "url": m["image"]["url"]}
                for m in product["media"]["nodes"]
            ],
            "images_csv": [i for i in images_csv if i]
        })

    print("✅ RESULTS:", len(results))

    return results


# 🔥 ASSOCIA IMMAGINI
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

    # assegna immagine alla variante
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