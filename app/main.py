import logging
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import APP_ENV, APP_URL, LOG_LEVEL, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_SCOPES
from app.db import consume_oauth_state, delete_shop, get_shop_token, init_db, save_oauth_state, save_shop_token
from app.logging_setup import configure_logging
from app.security import generate_state, is_valid_shop_domain, verify_shopify_hmac, verify_webhook_hmac
from app.services import (
    add_file_to_gallery,
    delete_product_media,
    get_products_page,
    remove_file_from_gallery,
    upload_files_to_product,
)

configure_logging(LOG_LEVEL)
logger = logging.getLogger(__name__)

app = FastAPI(title="Shopify Media Manager")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup() -> None:
    init_db()
    logger.info("Application startup complete", extra={"env": APP_ENV})


@app.get("/health")
def health():
    return {"ok": True, "env": APP_ENV}


@app.get("/install")
def install(shop: str = Query(...)) -> RedirectResponse:
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

    response = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
            "code": code,
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {response.text}")

    data = response.json()
    access_token = data.get("access_token")
    scope = data.get("scope")

    if not access_token:
        raise HTTPException(status_code=400, detail="No access token returned by Shopify")

    save_shop_token(shop, access_token, scope)
    logger.info("Shop installed", extra={"shop": shop, "scope": scope})

    return RedirectResponse(f"/?shop={shop}", status_code=302)


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


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    shop: str = Query(...),
    after: str | None = Query(None),
):
    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    token = get_shop_token(shop)
    if not token:
        return RedirectResponse(f"/install?shop={shop}", status_code=302)

    try:
        payload = get_products_page(shop=shop, first=12, after=after)
    except Exception as exc:
        logger.exception("Dashboard error", extra={"shop": shop})
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "shop": shop,
            "products": payload["products"],
            "page_info": payload["page_info"],
        },
    )


@app.post("/api/upload")
async def upload_to_product(
    shop: str = Form(...),
    product_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    uploaded = await upload_files_to_product(shop=shop, product_id=product_id, files=files)
    return {"ok": True, "files": uploaded}


@app.post("/api/gallery/add")
async def gallery_add(
    shop: str = Form(...),
    product_id: str = Form(...),
    file_id: str = Form(...),
):
    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    gallery = add_file_to_gallery(shop=shop, product_id=product_id, file_id=file_id)
    return {"ok": True, "gallery": gallery}


@app.post("/api/gallery/remove")
async def gallery_remove(
    shop: str = Form(...),
    product_id: str = Form(...),
    file_id: str = Form(...),
):
    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    gallery = remove_file_from_gallery(shop=shop, product_id=product_id, file_id=file_id)
    return {"ok": True, "gallery": gallery}


@app.post("/api/media/delete")
async def media_delete(
    shop: str = Form(...),
    product_id: str = Form(...),
    media_id: str = Form(...),
):
    if not is_valid_shop_domain(shop):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    deleted_ids = delete_product_media(shop=shop, product_id=product_id, media_id=media_id)
    return {"ok": True, "deleted_media_ids": deleted_ids}
