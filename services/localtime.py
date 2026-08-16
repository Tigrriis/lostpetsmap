"""Local time handling.

The site covers one state, so there is exactly one civil timezone to worry
about: Australia/Hobart. Everything is *stored* in UTC; this module is the
boundary that converts to and from what a person in Tasmania typed or reads.

The browser's ``datetime-local`` input sends a bare wall-clock string with no
offset. Interpreting that as UTC — the tempting one-liner — puts every report
11 hours out during daylight saving, which is exactly the window that matters
when someone is trying to work out how far a cat could have walked.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Australia/Hobart")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_local(value: datetime | None) -> datetime | None:
    """UTC (or naive-assumed-UTC) -> Australia/Hobart."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(TZ)


def parse_local_input(raw: str | None) -> datetime | None:
    """Parse a ``datetime-local`` value as Tasmanian wall time, return UTC.

    Accepts both "YYYY-MM-DDTHH:MM" and the seconds-bearing variant some
    browsers send.
    """
    if not raw:
        return None
    text = raw.strip().replace(" ", "T")
    try:
        naive = datetime.fromisoformat(text)
    except ValueError:
        return None
    if naive.tzinfo is not None:          # a browser that sent an offset anyway
        return naive.astimezone(timezone.utc)
    return naive.replace(tzinfo=TZ).astimezone(timezone.utc)


def to_input_value(value: datetime | None) -> str:
    """Format a stored UTC datetime for a ``datetime-local`` input."""
    local = to_local(value)
    return local.strftime("%Y-%m-%dT%H:%M") if local else ""


def format_local(value: datetime | None, fmt: str = "%-d %b %Y, %-I:%M %p") -> str:
    """Human-readable local time. Falls back to the Windows-safe format codes."""
    local = to_local(value)
    if local is None:
        return ""
    try:
        return local.strftime(fmt)
    except ValueError:
        # %-d / %-I are glibc extensions; Windows needs %#d / %#I.
        return local.strftime(fmt.replace("%-", "%#"))


def humanise_age(value: datetime | None) -> str:
    """"3 hours ago" / "12 days ago" — the number a searcher actually wants."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = now_utc() - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 3600:
        mins = seconds // 60
        return "just now" if mins < 1 else f"{mins} min ago"
    if seconds < 86_400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86_400
    if days < 31:
        return f"{days} day{'s' if days != 1 else ''} ago"
    months = days // 30
    return f"{months} month{'s' if months != 1 else ''} ago"
