import os
from dataclasses import dataclass

from sqlalchemy.engine import make_url
from dotenv import load_dotenv

load_dotenv()


def _normalize_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")

    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    if sslmode is not None:
        query.setdefault("ssl", sslmode)
    return url.set(query=query).render_as_string(hide_password=False)


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    webhook_secret: str
    webhook_url: str | None
    database_url: str
    admin_api_key: str
    health_check_token: str
    port: int
    scanner_retention_hours: int
    max_upload_mb: int
    default_currency: str
    tesseract_cmd: str | None


def get_settings() -> Settings:
    raw_database_url = os.getenv("DATABASE_URL", "")
    if not raw_database_url:
        raise RuntimeError("DATABASE_URL must be set to a persistent PostgreSQL database.")
    database_url = _normalize_database_url(raw_database_url)
    return Settings(
        bot_token=os.getenv("BOT_TOKEN") or None,
        webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
        webhook_url=os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL"),
        database_url=database_url,
        admin_api_key=os.getenv("ADMIN_API_KEY", ""),
        health_check_token=os.getenv("HEALTH_CHECK_TOKEN", ""),
        port=int(os.getenv("PORT", "10000")),
        scanner_retention_hours=max(1, int(os.getenv("SCANNER_RETENTION_HOURS", "24"))),
        max_upload_mb=max(1, int(os.getenv("MAX_UPLOAD_MB", "15"))),
        default_currency=os.getenv("DEFAULT_CURRENCY", "INR"),
        tesseract_cmd=os.getenv("TESSERACT_CMD") or None,
    )
