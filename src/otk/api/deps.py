"""Request-scoped dependencies: the store handle and bearer authentication."""

from __future__ import annotations

from fastapi import Depends, Request

from ..service import Principal, ServiceError, Store


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_principal(request: Request, store: Store = Depends(get_store)) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ServiceError(
            "missing_credentials",
            "provide an API key as 'Authorization: Bearer <token>'",
            401,
        )
    principal = store.authenticate(token.strip())

    # Rate limit per credential rather than per IP: clients sit behind their own
    # Odoo server, so an IP is usually shared by everyone at that company.
    bucket = principal.upload_token_id or f"key:{principal.api_key_id}"
    # Stashed for the middleware, which turns it into X-RateLimit-* headers.
    request.state.rate_limit = store.check_rate_limit(bucket)
    return principal


def require_api_key(principal: Principal = Depends(get_principal)) -> Principal:
    """Reject upload tokens: these routes are for the client's Odoo backend."""
    if principal.auth_type != "api_key":
        raise ServiceError(
            "insufficient_scope",
            "this endpoint requires an API key, not a single-use upload token",
            403,
        )
    return principal
