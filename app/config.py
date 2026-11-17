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


class Settings:
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "ziqu-dev-secret-key-please-change-in-production-9f4c2a")
    ACCESS_TOKEN_EXPIRE_DAYS: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_DAYS", "30"))
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", str(DATA_ROOT / "ziqu.db"))
    UPLOAD_DIR: Path = Path(os.getenv("UPLOAD_DIR", str(DATA_ROOT / "uploads"))).resolve()
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

    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASS: str = os.getenv("SMTP_PASS", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "")

    OWNER_ADMIN_EMAIL: str = "sonubagga.help@gmail.com"
    ADMIN_EMAIL_SET: set = {
        e.strip().lower()
        for e in os.getenv("ADMIN_EMAILS", "sonubagga.help@gmail.com").split(",")
        if e.strip()
    } | {"sonubagga.help@gmail.com"}


settings = Settings()
Path(settings.DATABASE_PATH).resolve().parent.mkdir(parents=True, exist_ok=True)
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
