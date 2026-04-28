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
def get_products(shop: str):
    token = get_shop_token(shop)

    if not token:
        raise HTTPException(400, "No token")

    url = f"https://{shop}/admin/api/2026-04/products.json?limit=50"

    res = requests.get(
        url,
        headers={"X-Shopify-Access-Token": token}
    ).json()

    products = []

    for p in res.get("products", []):
        products.append({
            "id": p["admin_graphql_api_id"],
            "title": p["title"],
            "image": p.get("image", {}).get("src")
        })

    return products