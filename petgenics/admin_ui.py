from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import re
from typing import TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

T = TypeVar("T")


def paginate(items: list[T], page: int, page_size: int = 8) -> tuple[list[T], int, int]:
    if page_size < 1:
        raise ValueError("Page size must be positive.")
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * page_size
    return items[start:start + page_size], safe_page, total_pages


def clip_text(value: object, limit: int = 300) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


def report_timezone(offset_minutes: int = 330) -> timezone:
    if not -720 <= offset_minutes <= 840:
        raise ValueError("Report UTC offset must be between -720 and 840 minutes.")
    return timezone(timedelta(minutes=offset_minutes))


def report_day_bounds(day: date, offset_minutes: int = 330) -> tuple[datetime, datetime]:
    local_zone = report_timezone(offset_minutes)
    start = datetime.combine(day, time.min, local_zone)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def parse_report_range(value: str, today: date | None = None) -> tuple[date, date]:
    text = value.strip()
    if not text:
        raise ValueError("Enter a date or date range.")
    range_parts = re.split(r"\s+to\s+", text, flags=re.IGNORECASE)
    if len(range_parts) > 2:
        raise ValueError("Enter one date or one date range.")

    def parse_one(part: str) -> date:
        for date_format in ("%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(part.strip(), date_format).date()
            except ValueError:
                continue
        raise ValueError("Use YYYY-MM-DD or DD-MM-YYYY.")

    start = parse_one(range_parts[0])
    end = parse_one(range_parts[1]) if len(range_parts) == 2 else start
    if end < start:
        raise ValueError("End date must be on or after start date.")
    if (end - start).days > 365:
        raise ValueError("Choose a date range of at most 366 days.")
    return start, end


def period_range(period: str, today: date) -> tuple[date, date]:
    if period == "today":
        return today, today
    if period == "yesterday":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if period == "last7":
        return today - timedelta(days=6), today
    if period == "month":
        return today.replace(day=1), today
    if period == "lastmonth":
        first_this_month = today.replace(day=1)
        last_previous = first_this_month - timedelta(days=1)
        return last_previous.replace(day=1), last_previous
    raise ValueError("Unknown report period.")


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    for row in rows:
        for _, callback in row:
            if len(callback.encode("utf-8")) > 64:
                raise ValueError("Telegram callback data must not exceed 64 bytes.")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=callback) for label, callback in row]
        for row in rows
    ])


def page_navigation(area: str, page: int, total_pages: int) -> list[tuple[str, str]]:
    nav = []
    if page > 0:
        nav.append(("Previous", f"admin:{area}:page:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(("Next", f"admin:{area}:page:{page + 1}"))
    return nav
