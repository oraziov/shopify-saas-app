from fastapi import FastAPI, Request, HTTPException
import requests

from app.config import SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET
from app.db import init_db, save_shop_token

app = FastAPI()   # 🔥 QUESTO DEVE ESSERE PRIMA DI TUTTO

@app.on_event("startup")
def startup():
    init_db()

@app.get("/callback")
def callback(request: Request):
    params = dict(request.query_params)

    shop = params.get("shop")
    code = params.get("code")

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

    data = response.json()
    token = data.get("access_token")

    if not token:
        raise HTTPException(400, detail="No token")

    save_shop_token(shop, token)

    return {"ok": True, "shop": shop}

@app.get("/test")
def test(shop: str):
    token = get_shop_token(shop)

    url = f"https://{shop}/admin/api/2026-04/products.json"

    res = requests.get(
        url,
        headers={
            "X-Shopify-Access-Token": token
        }
    )

    return res.json()