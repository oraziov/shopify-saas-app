from fastapi import FastAPI, Request, HTTPException, UploadFile, Form, File, Query
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode
import requests
import base64
import requests
import mimetypes
import time
import json
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import csv

templates = Jinja2Templates(directory="app/templates")

from app.config import SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, APP_URL
from app.db import init_db, save_shop_token, get_shop_token

app = FastAPI()


# 🔥 STARTUP
@app.on_event("startup")
def startup():
    init_db()


# 🔹 ROOT (per evitare Not Found)
@app.get("/")
def root():
    return {"status": "app running"}


# 🔹 INSTALL APP
@app.get("/install")
def install(shop: str = Query(...)):
    params = {
        "client_id": SHOPIFY_CLIENT_ID,
        "scope": "read_products",
        "redirect_uri": f"{APP_URL}/callback",
    }

    url = f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"

    return RedirectResponse(url)


# 🔹 CALLBACK SHOPIFY
@app.get("/callback")
def callback(request: Request):
    params = dict(request.query_params)

    shop = params.get("shop")
    code = params.get("code")

    if not shop or not code:
        raise HTTPException(status_code=400, detail="Missing shop or code")

    print("SHOP CALLBACK:", shop)

    response = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
            "code": code,
        },
    )

    print("TOKEN RESPONSE:", response.text)

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Token exchange failed: {response.text}",
        )

    data = response.json()
    token = data.get("access_token")

    if not token:
        raise HTTPException(status_code=400, detail="No token returned")

    save_shop_token(shop, token)

    return {
        "ok": True,
        "shop": shop,
        "message": "App installed successfully"
    }


# 🔹 TEST API SHOPIFY
@app.get("/test")
def test(shop: str = Query(...)):
    token = get_shop_token(shop)

    if not token:
        return {"error": "No token found. Install the app first."}

    url = f"https://{shop}/admin/api/2024-01/products.json"

    res = requests.get(
        url,
        headers={
            "X-Shopify-Access-Token": token
        }
    )

    return {
        "status": res.status_code,
        "response": res.json()
    }




@app.post("/upload")
async def upload_image(shop: str = Form(...), file: UploadFile = File(...)):
    token = get_shop_token(shop)

    if not token:
        raise HTTPException(400, "No token")

    content = await file.read()
    filename = file.filename or "upload.jpg"
    mime_type = file.content_type or mimetypes.guess_type(filename)[0] or "image/jpeg"

    # 1️⃣ STAGED UPLOAD
    staged_query = """
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
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

    staged_res = requests.post(
        f"https://{shop}/admin/api/2026-04/graphql.json",
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        },
        json={
            "query": staged_query,
            "variables": {
                "input": [{
                    "filename": filename,
                    "mimeType": mime_type,
                    "resource": "IMAGE",
                    "fileSize": str(len(content))
                }]
            }
        }
    ).json()

    target = staged_res["data"]["stagedUploadsCreate"]["stagedTargets"][0]

    # 2️⃣ UPLOAD FILE (PUT)
    upload_headers = {
        p["name"]: p["value"]
        for p in target["parameters"]
    }

    upload_res = requests.put(
        target["url"],
        data=content,
        headers=upload_headers
    )

    if upload_res.status_code not in [200, 201]:
        raise HTTPException(400, "Upload to Shopify failed")

    # 3️⃣ CREA FILE SU SHOPIFY
    file_create_query = """
    mutation fileCreate($files: [FileCreateInput!]!) {
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
          field
          message
        }
      }
    }
    """

    file_res = requests.post(
        f"https://{shop}/admin/api/2026-04/graphql.json",
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        },
        json={
            "query": file_create_query,
            "variables": {
                "files": [{
                    "originalSource": target["resourceUrl"],
                    "contentType": "IMAGE"
                }]
            }
        }
    ).json()

    file_data = file_res["data"]["fileCreate"]["files"][0]
    file_id = file_data["id"]

    # 4️⃣ WAIT UNTIL READY (FONDAMENTALE)
    status_query = """
    query ($id: ID!) {
      node(id: $id) {
        ... on MediaImage {
          id
          fileStatus
          image {
            url
          }
        }
      }
    }
    """

    for _ in range(10):
        check = requests.post(
            f"https://{shop}/admin/api/2026-04/graphql.json",
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json"
            },
            json={
                "query": status_query,
                "variables": {"id": file_id}
            }
        ).json()

        node = check.get("data", {}).get("node")

        if node and node.get("fileStatus") == "READY":
            return {
                "id": node["id"],
                "url": node["image"]["url"],
                "status": "READY"
            }

        time.sleep(1)

    # fallback
    return {
        "id": file_id,
        "status": "PROCESSING"
    }

@app.post("/attach")
def attach_image(shop: str = Form(...), product_id: str = Form(...), image_url: str = Form(...)):
    token = get_shop_token(shop)

    if not token:
        raise HTTPException(400, "No token")

    mutation = """
    mutation productCreateMedia($media: [CreateMediaInput!]!, $productId: ID!) {
      productCreateMedia(media: $media, productId: $productId) {
        media {
          ... on MediaImage {
            id
            image {
              url
            }
          }
        }
        mediaUserErrors {
          field
          message
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
        json={
            "query": mutation,
            "variables": {
                "productId": product_id,
                "media": [{
                    "originalSource": image_url,
                    "mediaContentType": "IMAGE"
                }]
            }
        }
    )

    return res.json()




@app.post("/gallery/add")
def add_to_gallery(
    shop: str = Form(...),
    product_id: str = Form(...),
    file_id: str = Form(...)
):
    token = get_shop_token(shop)

    if not token:
        raise HTTPException(400, "No token")

    # GET EXISTING
    query = """
    query ($id: ID!) {
      product(id: $id) {
        metafield(namespace: "custom", key: "gallery") {
          value
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
        json={"query": query, "variables": {"id": product_id}}
    ).json()

    print("METAFIELD RAW:", res)

    metafield = res.get("data", {}).get("product", {}).get("metafield")

    gallery = []

    if metafield and metafield.get("value"):
        try:
            gallery = json.loads(metafield["value"])
        except:
            gallery = []

    # ADD IMAGE
    if file_id not in gallery:
        gallery.append(file_id)

    # SAVE
    mutation = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        metafields {
          value
        }
        userErrors {
          message
        }
      }
    }
    """

    save = requests.post(
        f"https://{shop}/admin/api/2026-04/graphql.json",
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        },
        json={
            "query": mutation,
            "variables": {
                "metafields": [{
                    "ownerId": product_id,
                    "namespace": "custom",
                    "key": "gallery",
                    "type": "list.file_reference",
                    "value": json.dumps(gallery)
                }]
            }
        }
    ).json()

    print("SAVE RESPONSE:", save)

    return save


@app.get("/gallery/get")
def get_gallery(shop: str, product_id: str):
    token = get_shop_token(shop)

    if not token:
        raise HTTPException(400, "No token")

    query = """
    query ($id: ID!) {
      product(id: $id) {
        metafield(namespace: "custom", key: "gallery") {
          references(first: 20) {
            nodes {
              ... on MediaImage {
                id
                image {
                  url
                }
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
        json={
            "query": query,
            "variables": {"id": product_id}
        }
    ).json()

    nodes = (
        res.get("data", {})
        .get("product", {})
        .get("metafield", {})
        .get("references", {})
        .get("nodes", [])
    )

    gallery = [
        {
            "id": n["id"],
            "url": n["image"]["url"]
        }
        for n in nodes if n.get("image")
    ]

    return gallery




@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/gallery/reorder")
def reorder_gallery(
    shop: str = Form(...),
    product_id: str = Form(...),
    file_ids: str = Form(...)
):
    import json

    token = get_shop_token(shop)

    if not token:
        raise HTTPException(400, "No token")

    ids = json.loads(file_ids)

    mutation = """
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $metafields) {
        metafields {
          value
        }
        userErrors {
          message
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
        json={
            "query": mutation,
            "variables": {
                "metafields": [{
                    "ownerId": product_id,
                    "namespace": "custom",
                    "key": "gallery",
                    "type": "list.file_reference",
                    "value": json.dumps(ids)
                }]
            }
        }
    )

    return res.json()



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
                mediaContentType
                ... on MediaImage {
                  image {
                    url
                  }
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
        json={
            "query": gql,
            "variables": {"query": query}
        }
    ).json()

    products = []

    for edge in res.get("data", {}).get("products", {}).get("edges", []):
        node = edge["node"]

        images = [
            {
                "id": m["id"],
                "url": m["image"]["url"]
            }
            for m in node.get("media", {}).get("nodes", [])
            if m.get("image")
        ]

        variants = [
            {
                "id": v["id"],
                "title": v["title"]
            }
            for v in node.get("variants", {}).get("nodes", [])
        ]

        products.append({
            "id": node["id"],
            "title": node["title"],
            "images": images,
            "variants": variants
        })

    return products

@app.get("/product/images")
def get_product_images(shop: str, product_id: str):
    token = get_shop_token(shop)

    product_numeric = product_id.split("/")[-1]

    url = f"https://{shop}/admin/api/2026-04/products/{product_numeric}/images.json"

    res = requests.get(
        url,
        headers={"X-Shopify-Access-Token": token}
    ).json()

    return res.get("images", [])

@app.post("/upload-product-image")
async def upload_product_image(
    shop: str = Form(...),
    product_id: str = Form(...),
    file: UploadFile = File(...)
):
    # qui puoi riusare la tua funzione /upload
    # ma invece di restituire solo file id, dopo READY fai attach al prodotto
    uploaded = await upload_image(shop=shop, file=file)

    image_url = uploaded.get("url")
    if not image_url:
        raise HTTPException(400, "Upload failed")

    mutation = """
    mutation AddMedia($productId: ID!, $media: [CreateMediaInput!]!) {
      productCreateMedia(productId: $productId, media: $media) {
        media {
          id
          ... on MediaImage {
            image {
              url
            }
          }
        }
        mediaUserErrors {
          field
          message
        }
      }
    }
    """

    token = get_shop_token(shop)

    res = requests.post(
        f"https://{shop}/admin/api/2026-04/graphql.json",
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        },
        json={
            "query": mutation,
            "variables": {
                "productId": product_id,
                "media": [{
                    "originalSource": image_url,
                    "mediaContentType": "IMAGE"
                }]
            }
        }
    ).json()

    return res

@app.post("/product/media/delete")
def delete_product_media_endpoint(
    shop: str = Form(...),
    product_id: str = Form(...),
    media_id: str = Form(...)
):
    token = get_shop_token(shop)

    mutation = """
    mutation DeleteProductMedia($productId: ID!, $mediaIds: [ID!]!) {
      productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
        deletedMediaIds
        mediaUserErrors {
          field
          message
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
        json={
            "query": mutation,
            "variables": {
                "productId": product_id,
                "mediaIds": [media_id]
            }
        }
    ).json()

    return res


@app.post("/variant/image/set")
def set_variant_image(shop: str = Form(...), variant_id: str = Form(...), image_id: str = Form(...)):
    token = get_shop_token(shop)

    variant_numeric = variant_id.split("/")[-1]

    url = f"https://{shop}/admin/api/2026-04/variants/{variant_numeric}.json"

    res = requests.put(
        url,
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        },
        json={
            "variant": {
                "id": variant_numeric,
                "image_id": image_id
            }
        }
    )

    return res.json()



import csv

@app.post("/import-csv")
async def import_csv(
    shop: str = Form(...),
    file: UploadFile = File(...)
):
    token = get_shop_token(shop)

    if not token:
        raise HTTPException(400, "No token")

    contents = await file.read()

    with open("/tmp/import.csv", "wb") as f:
        f.write(contents)

    results = []

    with open("/tmp/import.csv", newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)

        for row in reader:

            handle = row.get("handle")
            sku = row.get("SKU")
            color_code = str(row.get("color_code"))

            if not handle or not sku:
                results.append({"error": "Missing handle or SKU"})
                continue

            # 🔥 1. TROVA PRODOTTO
            product_query = f"""
            {{
              products(first:1, query:"handle:{handle}") {{
                edges {{
                  node {{
                    id
                    variants(first:50) {{
                      edges {{
                        node {{
                          id
                          sku
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
                json={"query": product_query}
            ).json()

            edges = res.get("data", {}).get("products", {}).get("edges", [])

            if not edges:
                results.append({"handle": handle, "error": "Product not found"})
                continue

            product = edges[0]["node"]
            product_id = product["id"]
            product_numeric = product_id.split("/")[-1]

            # 🔥 2. TROVA VARIANTE
            variant_id = None

            for v in product["variants"]["edges"]:
                if v["node"]["sku"] == sku:
                    variant_id = v["node"]["id"]
                    break

            if not variant_id:
                results.append({"sku": sku, "error": "Variant not found"})
                continue

            variant_numeric = variant_id.split("/")[-1]

            # 🔥 3. UPLOAD IMMAGINI
            image_urls = [
                row.get("Image1"),
                row.get("Image2"),
                row.get("Image3")
            ]

            uploaded_ids = []

            for image_url in image_urls:

                if not image_url or not isinstance(image_url, str):
                    continue

                img_res = requests.post(
                    f"https://{shop}/admin/api/2026-04/products/{product_numeric}/images.json",
                    headers={
                        "X-Shopify-Access-Token": token,
                        "Content-Type": "application/json"
                    },
                    json={
                        "image": {
                            "src": image_url
                        }
                    }
                ).json()

                img = img_res.get("image")

                if img:
                    uploaded_ids.append(img["id"])

            # 🔥 4. ASSOCIA ALLA VARIANTE
            if uploaded_ids:
                requests.put(
                    f"https://{shop}/admin/api/2026-04/variants/{variant_numeric}.json",
                    headers={
                        "X-Shopify-Access-Token": token,
                        "Content-Type": "application/json"
                    },
                    json={
                        "variant": {
                            "id": variant_numeric,
                            "image_id": uploaded_ids[0]
                        }
                    }
                )

            # 🔥 5. METAFIELD MERGE (FIX IMPORTANTISSIMO)
            if uploaded_ids:

                # 👉 leggi esistente
                get_meta_query = """
                query ($id: ID!) {
                  product(id: $id) {
                    metafield(namespace: "custom", key: "color_gallery") {
                      value
                    }
                  }
                }
                """

                meta_res = requests.post(
                    f"https://{shop}/admin/api/2026-04/graphql.json",
                    headers={
                        "X-Shopify-Access-Token": token,
                        "Content-Type": "application/json"
                    },
                    json={
                        "query": get_meta_query,
                        "variables": {"id": product_id}
                    }
                ).json()

                metafield = meta_res.get("data", {}).get("product", {}).get("metafield")

                gallery = {}

                if metafield and metafield.get("value"):
                    try:
                        gallery = json.loads(metafield["value"])
                    except:
                        gallery = {}

                # 👉 merge
                if color_code not in gallery:
                    gallery[color_code] = []

                gallery[color_code].extend(uploaded_ids)

                # 👉 salva
                mutation = """
                mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
                  metafieldsSet(metafields: $metafields) {
                    metafields { value }
                    userErrors { message }
                  }
                }
                """

                requests.post(
                    f"https://{shop}/admin/api/2026-04/graphql.json",
                    headers={
                        "X-Shopify-Access-Token": token,
                        "Content-Type": "application/json"
                    },
                    json={
                        "query": mutation,
                        "variables": {
                            "metafields": [{
                                "ownerId": product_id,
                                "namespace": "custom",
                                "key": "color_gallery",
                                "type": "json",
                                "value": json.dumps(gallery)
                            }]
                        }
                    }
                )

            results.append({
                "handle": handle,
                "sku": sku,
                "status": "ok"
            })

    return {
        "status": "IMPORT COMPLETATO",
        "results": results
    }