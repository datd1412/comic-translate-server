from collections import defaultdict, deque
import logging
from threading import Lock
from time import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import settings
from auth_utils import decode_access_token
import auth_routes
import proxy_routes
import billing_routes

app = FastAPI(title=settings.PROJECT_NAME, version=settings.VERSION)
templates = Jinja2Templates(directory="templates")

_RATE_LIMIT_RULES = {
    ("POST", "/auth/firebase"): (12, 60),
    ("POST", "/auth/v1/token"): (20, 60),
    ("POST", "/billing/v1/payment-intents"): (20, 60),
    ("POST", "/api/v1/translate"): (40, 60),
}
_rate_limit_hits: dict[str, deque[float]] = defaultdict(deque)
_rate_limit_429_counts: dict[str, int] = defaultdict(int)
_rate_limit_lock = Lock()
_rate_limit_logger = logging.getLogger("rate_limit")


def _extract_access_token_from_request(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return request.cookies.get("ct_access_token")


def _rate_limit_subject(request: Request) -> str:
    client_ip = request.client.host if request.client else "unknown"

    # For translate traffic, prefer authenticated user id to avoid blocking users sharing one IP.
    if request.method.upper() == "POST" and request.url.path == "/api/v1/translate":
        token = _extract_access_token_from_request(request)
        if token:
            payload = decode_access_token(token)
            user_id = payload.get("sub") if payload else None
            if user_id:
                return f"user:{user_id}"

    return f"ip:{client_ip}"


def _cors_origins_from_settings() -> list[str]:
    return [
        origin.strip()
        for origin in (settings.CORS_ALLOW_ORIGINS or "").split(",")
        if origin.strip()
    ]

# Serve static assets for landing/login backgrounds and UI resources.
app.mount("/images", StaticFiles(directory="static/images"), name="images")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Define CORS to allow the desktop app (which might use localhost/127.0.0.1)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_from_settings(),
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)


@app.middleware("http")
async def auth_popup_compat_headers(request: Request, call_next):
    """Allow popup-based auth providers (Google/Firebase) to close correctly."""
    limit_config = _RATE_LIMIT_RULES.get((request.method.upper(), request.url.path))
    if limit_config:
        max_requests, window_seconds = limit_config
        endpoint = f"{request.method.upper()} {request.url.path}"
        subject = _rate_limit_subject(request)
        key = f"{endpoint}:{subject}"
        now = time()

        with _rate_limit_lock:
            hits = _rate_limit_hits[key]
            threshold = now - window_seconds
            while hits and hits[0] < threshold:
                hits.popleft()
            if len(hits) >= max_requests:
                _rate_limit_429_counts[endpoint] += 1
                _rate_limit_logger.warning(
                    "rate_limit_429 endpoint=%s subject=%s count=%s window_seconds=%s max_requests=%s",
                    endpoint,
                    subject,
                    _rate_limit_429_counts[endpoint],
                    window_seconds,
                    max_requests,
                )
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": {
                            "type": "RATE_LIMITED",
                            "message": "Too many requests. Please retry shortly.",
                        }
                    },
                )
            hits.append(now)

    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    if settings.ENABLE_SECURITY_HEADERS:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        if settings.ENABLE_HSTS:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# Landing page
@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})

# API status endpoint
@app.get("/api/status")
def api_status():
    return {
        "message": f"Welcome to {settings.PROJECT_NAME}",
        "status": "online",
        "free_credits_new_account": settings.DEFAULT_FREE_CREDITS
    }

# Mount Auth Routes
app.include_router(auth_routes.router, tags=["Authentication"])

# Mount Translation & OCR Logic
app.include_router(proxy_routes.router, tags=["Translation/OCR Proxy"])

# Mount Billing logic
app.include_router(billing_routes.router, tags=["Billing"])

if __name__ == "__main__":
    import uvicorn
    print(f"[Startup] Server starting on http://{settings.APP_HOST}:{settings.APP_PORT}")
    uvicorn.run("main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=True)
