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
    admin_telegram_ids: tuple[int, ...] = ()
    report_utc_offset_minutes: int = 330


def get_settings() -> Settings:
    raw_database_url = os.getenv("DATABASE_URL", "")
    if not raw_database_url:
        raise RuntimeError("DATABASE_URL must be set to a persistent PostgreSQL database.")
    database_url = _normalize_database_url(raw_database_url)
    try:
        admin_telegram_ids = tuple(
            dict.fromkeys(
                int(value.strip())
                for value in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
                if value.strip()
            )
        )
    except ValueError as exc:
        raise RuntimeError("ADMIN_TELEGRAM_IDS must be comma-separated numeric Telegram user IDs.") from exc
    if any(user_id <= 0 for user_id in admin_telegram_ids):
        raise RuntimeError("ADMIN_TELEGRAM_IDS must contain positive Telegram user IDs.")
    try:
        report_utc_offset_minutes = int(os.getenv("REPORT_UTC_OFFSET_MINUTES", "330"))
    except ValueError as exc:
        raise RuntimeError("REPORT_UTC_OFFSET_MINUTES must be an integer.") from exc
    if not -720 <= report_utc_offset_minutes <= 840:
        raise RuntimeError("REPORT_UTC_OFFSET_MINUTES must be between -720 and 840.")
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
        admin_telegram_ids=admin_telegram_ids,
        report_utc_offset_minutes=report_utc_offset_minutes,
    )
