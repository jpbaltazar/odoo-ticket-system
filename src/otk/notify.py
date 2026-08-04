"""Outbound alerts for tickets that should not wait in the inbox.

Deliberately one channel and stdlib-only. ntfy is a plain HTTP POST, so this
needs no dependency and no SDK, and a failure here must never affect the ticket
that triggered it — a client filing a problem should not care whether the
operator's phone is reachable.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import Settings

log = logging.getLogger("otk.notify")

# Ascending severity. `off` is not a priority a ticket can have; it is only
# meaningful as a threshold, and sorts above everything so nothing clears it.
PRIORITY_ORDER = ("low", "normal", "high", "urgent")
THRESHOLD_VALUES = (*PRIORITY_ORDER, "off")

# Our priority names happen to differ from ntfy's scale, so map explicitly
# rather than relying on the words coinciding.
_NTFY_PRIORITY = {"low": "low", "normal": "default", "high": "high", "urgent": "urgent"}
_NTFY_TAGS = {"urgent": "rotating_light", "high": "warning"}

_TIMEOUT_SECONDS = 10


def meets_threshold(priority: str, threshold: str) -> bool:
    """Whether a ticket at `priority` should alert given `threshold`."""
    if threshold == "off":
        return False
    try:
        return PRIORITY_ORDER.index(priority) >= PRIORITY_ORDER.index(threshold)
    except ValueError:
        # An unknown priority should surface rather than be silently dropped.
        return True


@dataclass(frozen=True)
class Alert:
    ref: str
    client_name: str
    priority: str
    title: str
    reporter: str
    url: str = ""


def build_message(alert: Alert, include_title: bool) -> tuple[str, str]:
    """Return (title, body) for the push.

    `include_title` exists because a ticket title is written by someone else's
    employee and can name a customer or an amount. Sent through a hosted ntfy
    it leaves your infrastructure, so a deployment can choose to push only the
    reference and look the rest up.
    """
    heading = f"{alert.priority.upper()} · {alert.ref}"
    if include_title:
        body = f"{alert.title}\n{alert.client_name} · {alert.reporter}"
    else:
        body = f"{alert.client_name} · {alert.reporter}\n(open the ticket for details)"
    return heading, body


def send(settings: Settings, alert: Alert) -> bool:
    """Push an alert. Returns whether it was delivered; never raises."""
    if not settings.notify_url:
        return False

    heading, body = build_message(alert, settings.notify_include_title)
    headers = {
        "Title": heading,
        "Priority": _NTFY_PRIORITY.get(alert.priority, "default"),
        "Content-Type": "text/plain; charset=utf-8",
    }
    if alert.priority in _NTFY_TAGS:
        headers["Tags"] = _NTFY_TAGS[alert.priority]
    if alert.url:
        headers["Click"] = alert.url
    if settings.notify_token:
        headers["Authorization"] = f"Bearer {settings.notify_token}"

    request = urllib.request.Request(
        settings.notify_url, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Swallowed on purpose: the ticket is already saved, and an unreachable
        # phone is not the filer's problem.
        log.warning("notification for %s failed: %s", alert.ref, exc)
        return False
