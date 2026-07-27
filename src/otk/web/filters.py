"""Presentation filters for the operator templates.

Three formats show up on every page — a file size, an age, and a timestamp —
and each has edge cases (missing values, rows that come back from SQLite as
strings) that a template is a miserable place to handle. They live here so the
markup can stay a description of the page instead of a pile of `{% if %}`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment

log = logging.getLogger("otk.web")

# Decimal units, matching what disk-usage tools and Odoo itself report. Binary
# units would show a 1,234,567-byte screenshot as "1.2 MiB", which reads as a
# different number to anyone comparing against `du`.
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

EMPTY = "—"


def _as_datetime(value: Any) -> datetime | None:
    """Coerce whatever the store handed the template into an aware datetime.

    Most values arrive as datetimes, but the storage page's purge candidates
    are raw `sqlite3.Row` dicts whose timestamps are still ISO strings. Naive
    values are read as UTC, which is the only thing this codebase ever writes.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def humanbytes(value: Any) -> str:
    """1234567 -> "1.2 MB"."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return EMPTY
    for unit in _UNITS:
        if abs(size) < 1000 or unit == _UNITS[-1]:
            # Whole bytes are never interesting as "512.0 B".
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000.0
    return f"{size:.1f} {_UNITS[-1]}"


def ago(value: Any) -> str:
    """Coarse relative age: "3h ago", "2d ago"."""
    moment = _as_datetime(value)
    if moment is None:
        return EMPTY
    seconds = (datetime.now(UTC) - moment).total_seconds()
    # Clock skew between the client's Odoo host and this one can put a
    # timestamp slightly in the future; "in -2s" would just look broken.
    if seconds < 60:
        return "just now"
    for limit, divisor, suffix in (
        (3600, 60, "m"),
        (86400, 3600, "h"),
        (2592000, 86400, "d"),
        (31536000, 604800, "w"),
    ):
        if seconds < limit:
            return f"{int(seconds // divisor)}{suffix} ago"
    return f"{int(seconds // 31536000)}y ago"


def _zone(tz_name: str) -> tzinfo | None:
    """Resolve an IANA name, falling back to the server's zone if it is bogus.

    A typo in OTK_TZ should not take the whole UI down over a timestamp.
    """
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("OTK_TZ=%r is not a known zone; using the server's", tz_name)
        return None


def make_localtime(tz_name: str = ""):
    """Build the `localtime` filter bound to a display zone.

    The abbreviation is always appended. The host and the operator are
    routinely in different countries — a German server and a Portuguese
    operator differ by exactly an hour all year — and an unlabelled "14:03"
    gives no hint that it might not be yours.
    """
    zone = _zone(tz_name)

    def localtime(value: Any) -> str:
        moment = _as_datetime(value)
        if moment is None:
            return EMPTY
        return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")

    return localtime


def register_filters(env: Environment, tz_name: str = "") -> None:
    env.filters["humanbytes"] = humanbytes
    env.filters["ago"] = ago
    env.filters["localtime"] = make_localtime(tz_name)
