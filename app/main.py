from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Query
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode
import requests
import base64
from fastapi import Form

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
async def upload_image(
    shop: str = Form(...),
    file: UploadFile = File(...)
):
    token = get_shop_token(shop)

    if not token:
        raise HTTPException(400, "No token")

    content = await file.read()
    b64 = base64.b64encode(content).decode()

    mutation = """
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

    url = f"https://{shop}/admin/api/2026-04/graphql.json"

    res = requests.post(
        url,
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        },
        json={
            "query": mutation,
            "variables": {
                "files": [
                    {
                        "contentType": "IMAGE",
                        "originalSource": f"data:image/jpeg;base64,{b64}"
                    }
                ]
            }
        }
    )

    return res.json()