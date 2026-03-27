import os
from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Use explicit path so .env is found regardless of CWD
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path, override=True)


def _build_callback_url(env_key: str, path_suffix: str) -> str:
    explicit = os.getenv(env_key, "").strip()
    if explicit:
        return explicit

    public_base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base:
        return f"{public_base}{path_suffix}"

    return ""

class Settings(BaseSettings):
    # App Settings
    PROJECT_NAME: str = "ContextRead Server"
    VERSION: str = "1.0.0"
    
    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./comic_server.db")

    # Web security and CORS
    CORS_ALLOW_ORIGINS: str = os.getenv("CORS_ALLOW_ORIGINS", "")
    CORS_ALLOW_CREDENTIALS: bool = os.getenv("CORS_ALLOW_CREDENTIALS", "true").lower() == "true"
    ENABLE_SECURITY_HEADERS: bool = os.getenv("ENABLE_SECURITY_HEADERS", "true").lower() == "true"
    ENABLE_HSTS: bool = os.getenv("ENABLE_HSTS", "false").lower() == "true"

    # Runtime server options (main.py __main__ mode)
    APP_HOST: str = os.getenv("APP_HOST", "127.0.0.1")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
    
    # Auth & JWT Settings
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    SESSION_COOKIE_SECURE: bool = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    SESSION_COOKIE_SAMESITE: str = os.getenv("SESSION_COOKIE_SAMESITE", "lax")
    SESSION_COOKIE_DOMAIN: str = os.getenv("SESSION_COOKIE_DOMAIN", "")
    
    # Credits System
    DEFAULT_FREE_CREDITS: int = 100
    
    # AI API Keys (Read from ENV or set default here)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    DEEPSEEK_PROVIDER: str = os.getenv("DEEPSEEK_PROVIDER", "auto")
    DEEPSEEK_NVIDIA_CHAT_MODEL: str = os.getenv("DEEPSEEK_NVIDIA_CHAT_MODEL", "deepseek-ai/deepseek-v3.2")
    DEEPSEEK_NVIDIA_REASONER_MODEL: str = os.getenv("DEEPSEEK_NVIDIA_REASONER_MODEL", "deepseek-ai/deepseek-r1")
    DEEPSEEK_TIMEOUT_SECONDS: float = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "120"))
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    CLAUDE_API_KEY: str = os.getenv("CLAUDE_API_KEY", "")

    # Firebase Auth
    FIREBASE_API_KEY: str = os.getenv("FIREBASE_API_KEY", "")
    FIREBASE_AUTH_DOMAIN: str = os.getenv("FIREBASE_AUTH_DOMAIN", "")
    FIREBASE_PROJECT_ID: str = os.getenv("FIREBASE_PROJECT_ID", "")
    FIREBASE_STORAGE_BUCKET: str = os.getenv("FIREBASE_STORAGE_BUCKET", "")
    FIREBASE_MESSAGING_SENDER_ID: str = os.getenv("FIREBASE_MESSAGING_SENDER_ID", "")
    FIREBASE_APP_ID: str = os.getenv("FIREBASE_APP_ID", "")

    # PayOS billing
    PAYOS_CLIENT_ID: str = os.getenv("PAYOS_CLIENT_ID", "")
    PAYOS_API_KEY: str = os.getenv("PAYOS_API_KEY", "")
    PAYOS_CHECKSUM_KEY: str = os.getenv("PAYOS_CHECKSUM_KEY", "")
    PAYOS_RETURN_URL: str = _build_callback_url("PAYOS_RETURN_URL", "/billing/v1/payos/return")
    PAYOS_CANCEL_URL: str = _build_callback_url("PAYOS_CANCEL_URL", "/billing/v1/payos/cancel")
    PAYOS_EXPIRE_MINUTES: int = int(os.getenv("PAYOS_EXPIRE_MINUTES", "15"))

    # Admin access (comma-separated emails)
    ADMIN_EMAILS: str = os.getenv("ADMIN_EMAILS", "")

    # VNPay billing
    VNPAY_TMN_CODE: str = os.getenv("VNPAY_TMN_CODE", "")
    VNPAY_HASH_SECRET: str = os.getenv("VNPAY_HASH_SECRET", "")
    VNPAY_PAYMENT_URL: str = os.getenv("VNPAY_PAYMENT_URL", "https://sandbox.vnpayment.vn/paymentv2/vpcpay.html")
    VNPAY_RETURN_URL: str = _build_callback_url("VNPAY_RETURN_URL", "/billing/v1/vnpay/return")
    VNPAY_LOCALE: str = os.getenv("VNPAY_LOCALE", "vn")
    VNPAY_ORDER_TYPE: str = os.getenv("VNPAY_ORDER_TYPE", "other")
    VNPAY_EXPIRE_MINUTES: int = int(os.getenv("VNPAY_EXPIRE_MINUTES", "15"))

    class Config:
        env_file = ".env"

settings = Settings()
