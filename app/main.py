import logging
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    API_VERSION,
    APP_ENV,
    APP_URL,
    LOG_LEVEL,
    SHOPIFY_CLIENT_ID,
    SHOPIFY_CLIENT_SECRET,
    SHOPIFY_SCOPES,
)

from app.db import (
    consume_oauth_state,
    delete_shop,
    get_shop_token,
    init_db,
    save_oauth_state,
    save_shop_token,
)

from app.logging_setup import configure_logging
from app.security import (
    generate_state,
    is_valid_shop_domain,
    verify_shopify_hmac,
    verify_webhook_hmac,
)

from app.services import get_products_full


# 🔧 Logging
configure_logging(LOG_LEVEL)
logger = logging.getLogger(__name__)

# 🚀 FastAPI app
app = FastAPI(title="Shopify SaaS OAuth App")

templates = Jinja2Templates(directory="app/templates")


# 🔄 Startup
@app.on_event("startup")
def startup() -> None:
    init_db()
    logger.info("Application startup complete", extra={"env": APP_ENV})


# ❤️ Health check
@app.get("/health")
def health():
    return {"ok": True, "env": APP_ENV}


# 🔐 INSTALL (OAuth start)
@app.get("/install")
def install(shop: str = Query(...)):

    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET or not APP_URL:
        raise HTTPException(status_code=500, detail="Missing Shopify config")

    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    state = generate_state()
    save_oauth_state(state, shop)

    params = {
        "client_id": SHOPIFY_CLIENT_ID,
        "scope": SHOPIFY_SCOPES,
        "redirect_uri": f"{APP_URL}/callback",
        "state": state,
    }

    auth_url = f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"

    logger.info("Redirecting to Shopify OAuth", extra={"shop": shop})

    return RedirectResponse(auth_url)


# 🔑 CALLBACK (token exchange)
@app.get("/callback")
def callback(request: Request):

    params = dict(request.query_params)

    shop = params.get("shop")
    code = params.get("code")
    state = params.get("state")
    hmac_value = params.get("hmac")

    if not shop or not code or not state or not hmac_value:
        raise HTTPException(status_code=400, detail="Missing OAuth params")

    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    if not verify_shopify_hmac(params, hmac_value):
        raise HTTPException(status_code=400, detail="Invalid HMAC")

    if not consume_oauth_state(state, shop):
        raise HTTPException(status_code=400, detail="Invalid state")

    # 🔄 Exchange token
    token_url = f"https://{shop}/admin/oauth/access_token"

    response = requests.post(
        token_url,
        json={
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
            "code": code,
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Token exchange failed")

    data = response.json()

    access_token = data.get("access_token")
    scope = data.get("scope")

    if not access_token:
        raise HTTPException(status_code=400, detail="No access token")

    save_shop_token(shop, access_token, scope)

    logger.info("Shop installed", extra={"shop": shop})

    return RedirectResponse(f"/?shop={shop}")


# 🧹 WEBHOOK uninstall
@app.post("/webhooks/app/uninstalled")
async def app_uninstalled(request: Request):

    raw_body = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    shop = request.headers.get("X-Shopify-Shop-Domain")

    if not verify_webhook_hmac(raw_body, hmac_header):
        raise HTTPException(status_code=401, detail="Invalid webhook HMAC")

    if shop and is_valid_shop_domain(shop):
        delete_shop(shop)
        logger.info("Shop removed", extra={"shop": shop})

    return {"ok": True}


# 🖥️ DASHBOARD
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, shop: str = Query(...)):

    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    token = get_shop_token(shop)

    if not token:
        return RedirectResponse(f"/install?shop={shop}")

    try:
        products = get_products_full(
            limit=10,
            shop=shop
        )

    except Exception as e:
        logger.exception("Dashboard error", extra={"shop": shop})
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "products": products,
            "shop": shop,
        },
    )
