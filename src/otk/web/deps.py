"""Session dependency and CSRF enforcement for the operator UI."""

from __future__ import annotations

import hmac

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse

from ..auth import OperatorPrincipal
from ..service import ServiceError, Store

SESSION_COOKIE = "otk_session"


class NotSignedIn(Exception):
    """Raised instead of a 401 so the handler can redirect to the login page."""

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


def get_store(request: Request) -> Store:
    return request.app.state.store


def require_operator(
    request: Request, store: Store = Depends(get_store)
) -> OperatorPrincipal:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie:
        raise NotSignedIn(request.url.path)
    try:
        principal = store.authenticate_session(cookie)
    except ServiceError as exc:
        raise NotSignedIn(request.url.path) from exc
    request.state.operator = principal
    return principal


async def require_csrf(
    request: Request, operator: OperatorPrincipal = Depends(require_operator)
) -> OperatorPrincipal:
    """Reject a state-changing request without a matching CSRF token.

    The session cookie is SameSite=Lax, which already blocks cross-site form
    POSTs in current browsers. This is the belt to that's braces: it also
    catches a same-site subdomain takeover, which SameSite would not.
    """
    form = await request.form()
    submitted = str(form.get("csrf_token", ""))
    if not submitted or not hmac.compare_digest(submitted, operator.csrf_token):
        raise ServiceError("csrf_failed", "form token missing or stale; reload and retry", 403)
    return operator


def redirect_to_login(next_url: str = "/") -> RedirectResponse:
    from urllib.parse import quote

    target = "/login" + (f"?next={quote(next_url, safe='')}" if next_url != "/" else "")
    return RedirectResponse(target, status_code=303)
