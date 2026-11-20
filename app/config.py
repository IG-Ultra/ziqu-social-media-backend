import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _default_data_dir() -> Path:
    render_disk = os.getenv("RENDER_DISK_PATH", "").strip() or os.getenv("DATA_DIR", "").strip()
    if render_disk:
        p = Path(render_disk)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass
    return BASE_DIR


DATA_ROOT = _default_data_dir()


def _normalize_database_url(raw: str) -> str:
    url = raw.strip().strip("'\"").strip()
    if url.upper().startswith("DATABASE_URL="):
        url = url.split("=", 1)[1].strip().strip("'\"").strip()
    if url.lower().startswith("psql "):
        url = url[5:].strip().strip("'\"").strip()
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def _parse_cloudinary_env() -> tuple[str, str, str]:
    cloud = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip().strip("'\"")
    key = os.getenv("CLOUDINARY_API_KEY", "").strip().strip("'\"")
    secret = os.getenv("CLOUDINARY_API_SECRET", "").strip().strip("'\"")
    raw_url = os.getenv("CLOUDINARY_URL", "").strip().strip("'\"").strip()
    if raw_url.upper().startswith("CLOUDINARY_URL="):
        raw_url = raw_url.split("=", 1)[1].strip().strip("'\"").strip()
    # Format: cloudinary://<api_key>:<api_secret>@<cloud_name>
    if raw_url.startswith("cloudinary://") and "@" in raw_url:
        try:
            creds_part, cloud_part = raw_url[len("cloudinary://"):].split("@", 1)
            if ":" in creds_part:
                parsed_key, parsed_secret = creds_part.split(":", 1)
                cloud = cloud or cloud_part.split("/", 1)[0].strip()
                key = key or parsed_key.strip()
                secret = secret or parsed_secret.strip()
        except Exception:
            pass
    return cloud, key, secret


_CLOUD_NAME, _CLOUD_KEY, _CLOUD_SECRET = _parse_cloudinary_env()


class Settings:
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "ziqu-dev-secret-key-please-change-in-production-9f4c2a")
    ACCESS_TOKEN_EXPIRE_DAYS: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_DAYS", "30"))
    DATABASE_URL: str = _normalize_database_url(os.getenv("DATABASE_URL", ""))
    DB_POOL_MIN: int = max(1, int(os.getenv("DB_POOL_MIN", "2")))
    DB_POOL_MAX: int = max(4, int(os.getenv("DB_POOL_MAX", "20")))
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", str(DATA_ROOT / "ziqu.db"))
    UPLOAD_DIR: Path = Path(os.getenv("UPLOAD_DIR", str(DATA_ROOT / "uploads"))).resolve()
    CLOUDINARY_CLOUD_NAME: str = _CLOUD_NAME
    CLOUDINARY_API_KEY: str = _CLOUD_KEY
    CLOUDINARY_API_SECRET: str = _CLOUD_SECRET
    PUBLIC_BASE_URL: str = (
        os.getenv("PUBLIC_BASE_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    ).rstrip("/")
    KEEP_ALIVE_URL: str = (
        os.getenv("KEEP_ALIVE_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
        or PUBLIC_BASE_URL
    ).rstrip("/")
    KEEP_ALIVE_INTERVAL_SECONDS: int = max(25, int(os.getenv("KEEP_ALIVE_INTERVAL_SECONDS", "55")))
    KEEP_ALIVE_ENABLED: bool = os.getenv("KEEP_ALIVE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

    SMTP_HOST: str = os.getenv("SMTP_HOST", "").strip()
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "").strip()
    SMTP_PASS: str = os.getenv("SMTP_PASS", "").strip()
    SMTP_FROM: str = os.getenv("SMTP_FROM", "").strip()
    SMTP_USE_SSL: bool = os.getenv("SMTP_USE_SSL", "").strip().lower() in {"1", "true", "yes", "on"}
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "").strip()
    RESEND_FROM: str = os.getenv("RESEND_FROM", "").strip()
    BREVO_API_KEY: str = os.getenv("BREVO_API_KEY", "").strip()
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "").strip()
    APP_RESET_DEEP_LINK_SCHEME: str = os.getenv("APP_RESET_DEEP_LINK_SCHEME", "ziqu").strip() or "ziqu"

    OWNER_ADMIN_EMAIL: str = "sonubagga.help@gmail.com"
    ADMIN_EMAIL_SET: set = {
        e.strip().lower()
        for e in os.getenv("ADMIN_EMAILS", "sonubagga.help@gmail.com").split(",")
        if e.strip()
    } | {"sonubagga.help@gmail.com"}


settings = Settings()
Path(settings.DATABASE_PATH).resolve().parent.mkdir(parents=True, exist_ok=True)
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

