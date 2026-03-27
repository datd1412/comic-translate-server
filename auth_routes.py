from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import httpx

from database import get_db, User, UserSession
from auth_utils import (
    create_access_token, create_refresh_token, decode_access_token
)
from config import settings

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# This is a dummy OAuth2 scheme just to extract token from header
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/v1/token", auto_error=False)


def _admin_email_set() -> set[str]:
    return {
        email.strip().lower()
        for email in (settings.ADMIN_EMAILS or "").split(",")
        if email.strip()
    }


def _set_session_cookies(response: JSONResponse, access_token: str, refresh_token: str):
    cookie_domain = settings.SESSION_COOKIE_DOMAIN or None
    samesite = (settings.SESSION_COOKIE_SAMESITE or "lax").lower()

    response.set_cookie(
        key="ct_access_token",
        value=access_token,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite=samesite,
        domain=cookie_domain,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="ct_refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite=samesite,
        domain=cookie_domain,
        path="/",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    )


def _clear_session_cookies(response: JSONResponse):
    cookie_domain = settings.SESSION_COOKIE_DOMAIN or None
    response.delete_cookie("ct_access_token", path="/", domain=cookie_domain)
    response.delete_cookie("ct_refresh_token", path="/", domain=cookie_domain)


def _extract_token(request: Request, bearer_token: str | None) -> str | None:
    if bearer_token:
        return bearer_token
    return request.cookies.get("ct_access_token")

def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    token_value = _extract_token(request, token)
    if not token_value:
        raise HTTPException(status_code=401, detail="Missing access token")

    payload = decode_access_token(token_value)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    user_id = payload.get("sub")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
        
    return user


def get_current_user_optional(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    token_value = _extract_token(request, token)
    if not token_value:
        return None

    payload = decode_access_token(token_value)
    if not payload:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    return db.query(User).filter(User.id == user_id).first()

# -------------------------------------------------------------
# WEB LOGIN PAGE (Firebase Auth handles everything client-side)
# -------------------------------------------------------------
@router.get("/login/", response_class=HTMLResponse)
async def login_page(request: Request, request_id: str | None = None, desktop_callback_uri: str | None = None):
    # Treat blank-like query values as missing so web login can fall back to landing redirect.
    if isinstance(request_id, str):
        request_id = request_id.strip() or None
    if isinstance(desktop_callback_uri, str):
        normalized = desktop_callback_uri.strip()
        desktop_callback_uri = None if normalized.lower() in {"", "none", "null", "undefined"} else normalized

    return templates.TemplateResponse(
        "login.html", 
        {
            "request": request, 
            "request_id": request_id, 
            "desktop_callback_uri": desktop_callback_uri,
            "firebase_api_key": settings.FIREBASE_API_KEY,
            "firebase_auth_domain": settings.FIREBASE_AUTH_DOMAIN,
            "firebase_project_id": settings.FIREBASE_PROJECT_ID,
            "firebase_storage_bucket": settings.FIREBASE_STORAGE_BUCKET,
            "firebase_messaging_sender_id": settings.FIREBASE_MESSAGING_SENDER_ID,
            "firebase_app_id": settings.FIREBASE_APP_ID,
        }
    )

# -------------------------------------------------------------
# FIREBASE TOKEN VERIFICATION
# Firebase JS SDK authenticates user → sends ID token here
# Backend verifies token, creates/finds local user, issues JWT
# -------------------------------------------------------------
async def verify_firebase_token(id_token: str) -> dict:
    """Verify Firebase ID token via Google's tokeninfo endpoint."""
    async with httpx.AsyncClient() as client:
        # Use Firebase Auth REST API to get user info from ID token
        resp = await client.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={settings.FIREBASE_API_KEY}",
            json={"idToken": id_token}
        )
    if resp.status_code != 200:
        return None
    
    data = resp.json()
    users = data.get("users", [])
    if not users:
        return None
    
    firebase_user = users[0]
    return {
        "email": firebase_user.get("email"),
        "email_verified": firebase_user.get("emailVerified", False),
        "provider": firebase_user.get("providerUserInfo", [{}])[0].get("providerId", "password"),
    }

@router.post("/auth/firebase")
async def firebase_auth(request: Request, db: Session = Depends(get_db)):
    """
    Frontend sends: { id_token, request_id, desktop_callback_uri }
    Backend verifies Firebase token, creates/finds user, returns callback page.
    """
    data = await request.json()
    id_token = data.get("id_token")
    request_id = data.get("request_id")
    desktop_callback_uri = data.get("desktop_callback_uri")

    if isinstance(request_id, str):
        request_id = request_id.strip() or None
    if isinstance(desktop_callback_uri, str):
        normalized = desktop_callback_uri.strip()
        desktop_callback_uri = None if normalized.lower() in {"", "none", "null", "undefined"} else normalized

    if not id_token:
        return JSONResponse({"error": "Missing Firebase ID token"}, status_code=400)

    # Verify with Firebase
    firebase_user = await verify_firebase_token(id_token)
    if not firebase_user or not firebase_user.get("email"):
        return JSONResponse({"error": "Token không hợp lệ hoặc đã hết hạn"}, status_code=401)

    email = firebase_user["email"].lower()
    provider = "google" if "google" in firebase_user.get("provider", "") else "email"

    # For email/password accounts, require verified email before allowing app session.
    if provider == "email" and not firebase_user.get("email_verified", False):
        return JSONResponse(
            {"error": "Email chưa được xác minh. Vui lòng xác minh email rồi đăng nhập lại."},
            status_code=403
        )

    # Find or create local user
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            email=email,
            password_hash=None,
            auth_provider=provider,
            credits_total=settings.DEFAULT_FREE_CREDITS,
            credits_one_time=settings.DEFAULT_FREE_CREDITS,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    # Create our own JWT tokens for the desktop app
    access_token = create_access_token(data={"sub": user.id, "email": user.email})
    refresh_token = create_refresh_token(data={"sub": user.id})

    session = UserSession(
        user_id=user.id,
        refresh_token=refresh_token,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )
    db.add(session)
    db.commit()

    response = JSONResponse({
        "success": True,
        "request_id": request_id,
        "desktop_callback_uri": desktop_callback_uri,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_info": {
            "email": user.email,
            "tier": user.tier,
            "credits": {
                "subscription": user.credits_subscription,
                "one_time": user.credits_one_time,
                "total": user.credits_total
            },
            "monthly_credits": user.monthly_credits
        }
    })
    _set_session_cookies(response, access_token, refresh_token)
    return response

# -------------------------------------------------------------
# API ENDPOINTS CALLED BY DESKTOP APP
# -------------------------------------------------------------

@router.post("/auth/v1/token")
async def refresh_token(request: Request, db: Session = Depends(get_db)):
    # Desktop app sends: {"refresh_token": "..."}
    data = await request.json()
    refresh_token = data.get("refresh_token") or request.cookies.get("ct_refresh_token")
    
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Missing refresh token")
        
    session = db.query(UserSession).filter(UserSession.refresh_token == refresh_token).first()
    if not session:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if session.expires_at and session.expires_at < datetime.utcnow():
        db.delete(session)
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token expired")
        
    user = session.user
    if not user:
        db.delete(session)
        db.commit()
        raise HTTPException(status_code=401, detail="Session user not found")

    new_access_token = create_access_token(data={"sub": user.id, "email": user.email})
    new_refresh_token = create_refresh_token(data={"sub": user.id})
    
    # Update session
    session.refresh_token = new_refresh_token
    session.expires_at = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    db.commit()
    
    response = JSONResponse({
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    })
    _set_session_cookies(response, new_access_token, new_refresh_token)
    return response

@router.get("/auth/v1/validate")
async def validate_token(user: User = Depends(get_current_user)):
    # If the dependency succeeds, token is valid
    return {"valid": True, "message": "Token is valid"}

@router.post("/auth/v1/signout")
async def signout(request: Request, user: User | None = Depends(get_current_user_optional), db: Session = Depends(get_db)):
    refresh_token = request.cookies.get("ct_refresh_token")
    scope = request.query_params.get("scope", "web")

    # Allow desktop clients to pass refresh_token in JSON body.
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not refresh_token:
        refresh_token = payload.get("refresh_token")

    if scope == "all" and user:
        db.query(UserSession).filter(UserSession.user_id == user.id).delete(synchronize_session=False)
    elif refresh_token:
        db.query(UserSession).filter(UserSession.refresh_token == refresh_token).delete(synchronize_session=False)
    elif user:
        db.query(UserSession).filter(UserSession.user_id == user.id).delete(synchronize_session=False)

    db.commit()

    response = JSONResponse({"status": "ok"})
    _clear_session_cookies(response)
    return response

@router.get("/auth/v1/user_info")
async def get_user_info(user: User = Depends(get_current_user)):
    user.update_total_credits()
    is_admin = user.email.lower() in _admin_email_set()
    return {
         "email": user.email,
         "is_admin": is_admin,
         "tier": user.tier,
         "credits": {
             "subscription": user.credits_subscription,
             "one_time": user.credits_one_time,
             "total": user.credits_total
         },
         "monthly_credits": user.monthly_credits
    }
