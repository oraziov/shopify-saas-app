import logging
from urllib.parse import urlencode

import requests
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import (
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
    create_csrf_token,
    generate_state,
    is_valid_shop_domain,
    require_csrf_token,
    verify_shopify_hmac,
    verify_shopify_session_token,
    verify_webhook_hmac,
)
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


MAX_FILES = 8
MAX_FILE_SIZE_MB = 15
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

ALLOWED_UPLOAD_TYPES = {
    "image/jpeg": [".jpg", ".jpeg"],
    "image/png": [".png"],
    "image/webp": [".webp"],
    "image/gif": [".gif"],
    "video/mp4": [".mp4"],
}


@app.on_event("startup")
def startup() -> None:
    init_db()
    logger.info("Application startup complete", extra={"env": APP_ENV})


@app.get("/health")
def health():
    return {"ok": True, "env": APP_ENV}


def get_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    return authorization[len(prefix):].strip()


def require_shopify_auth(
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    token = get_bearer_token(authorization)
    session = verify_shopify_session_token(token)

    shop = session.get("shop")
    if not shop or not is_valid_shop_domain(shop):
        raise HTTPException(status_code=401, detail="Invalid Shopify session")

    access_token = get_shop_token(shop)
    if not access_token:
        raise HTTPException(status_code=401, detail="Shop not installed")

    return {
        "shop": shop,
        "session": session,
        "access_token": access_token,
    }


async def validate_upload_files(files: list[UploadFile]) -> None:
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_FILES} files allowed")

    for file in files:
        if not file.filename:
            raise HTTPException(status_code=400, detail="Invalid filename")

        filename = file.filename.lower()
        content_type = file.content_type

        if content_type not in ALLOWED_UPLOAD_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {content_type}",
            )

        allowed_extensions = ALLOWED_UPLOAD_TYPES[content_type]
        if not any(filename.endswith(ext) for ext in allowed_extensions):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file extension for {file.filename}",
            )

        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_SIZE_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"{file.filename} exceeds {MAX_FILE_SIZE_MB}MB",
                )

        await file.seek(0)


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
        timeout=10,
    )

    if response.status_code != 200:
        logger.warning(
            "Token exchange failed",
            extra={"shop": shop, "status_code": response.status_code},
        )
        raise HTTPException(status_code=400, detail="Token exchange failed")

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

    csrf_token = create_csrf_token(shop)

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
            "csrf_token": csrf_token,
            "products": payload["products"],
            "page_info": payload["page_info"],
        },
    )


@app.get("/api/csrf")
def get_csrf(auth=Depends(require_shopify_auth)):
    return {
        "ok": True,
        "csrf_token": create_csrf_token(auth["shop"]),
    }


@app.post("/api/upload")
async def upload_to_product(
    product_id: str = Form(...),
    files: list[UploadFile] = File(...),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    auth=Depends(require_shopify_auth),
):
    shop = auth["shop"]

    require_csrf_token(x_csrf_token, shop)
    await validate_upload_files(files)

    try:
        uploaded = await upload_files_to_product(
            shop=shop,
            product_id=product_id,
            files=files,
        )
    except Exception:
        logger.exception("Upload failed", extra={"shop": shop, "product_id": product_id})
        raise HTTPException(status_code=500, detail="Upload failed")

    return {"ok": True, "files": uploaded}


@app.post("/api/gallery/add")
async def gallery_add(
    product_id: str = Form(...),
    file_id: str = Form(...),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    auth=Depends(require_shopify_auth),
):
    shop = auth["shop"]

    require_csrf_token(x_csrf_token, shop)

    try:
        gallery = add_file_to_gallery(shop=shop, product_id=product_id, file_id=file_id)
    except Exception:
        logger.exception("Gallery add failed", extra={"shop": shop, "product_id": product_id})
        raise HTTPException(status_code=500, detail="Gallery update failed")

    return {"ok": True, "gallery": gallery}


@app.post("/api/gallery/remove")
async def gallery_remove(
    product_id: str = Form(...),
    file_id: str = Form(...),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    auth=Depends(require_shopify_auth),
):
    shop = auth["shop"]

    require_csrf_token(x_csrf_token, shop)

    try:
        gallery = remove_file_from_gallery(shop=shop, product_id=product_id, file_id=file_id)
    except Exception:
        logger.exception("Gallery remove failed", extra={"shop": shop, "product_id": product_id})
        raise HTTPException(status_code=500, detail="Gallery update failed")

    return {"ok": True, "gallery": gallery}


@app.post("/api/media/delete")
async def media_delete(
    product_id: str = Form(...),
    media_id: str = Form(...),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    auth=Depends(require_shopify_auth),
):
    shop = auth["shop"]

    require_csrf_token(x_csrf_token, shop)

    try:
        deleted_ids = delete_product_media(
            shop=shop,
            product_id=product_id,
            media_id=media_id,
        )
    except Exception:
        logger.exception("Media delete failed", extra={"shop": shop, "media_id": media_id})
        raise HTTPException(status_code=500, detail="Media delete failed")

    return {"ok": True, "deleted_media_ids": deleted_ids}