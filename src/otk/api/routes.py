"""API v1 routes.

Ticket creation accepts either `application/json` (attachments inline as
base64) or `multipart/form-data` (attachments as file parts). The JSON form is
the easy one to call from Odoo's Python backend; multipart avoids the 33%
base64 overhead for large screenshots and is what the browser flow should use.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ..schemas import (
    CommentCreate,
    CommentList,
    CommentOut,
    ErrorOut,
    InlineFile,
    TicketCreate,
    TicketList,
    TicketOut,
    TicketUpdate,
    UploadTokenOut,
    UploadTokenRequest,
    WhoAmI,
)
from ..service import IncomingFile, Principal, ServiceError, Store, TicketFilters, now
from .deps import get_principal, get_store, require_api_key
from .serializers import attachment_out, comment_out, incoming_from_inline, ticket_out

router = APIRouter(prefix="/api/v1")

MAX_WAIT_SECONDS = 30
"""Ceiling on a long poll. Kept well under the 60s most proxies default to,
so a held connection is never cut by something in the middle."""

_POLL_INTERVAL = 1.0


async def _long_poll(fetch: Callable[[], Any], wait: int, request: Request) -> Any:
    """Call `fetch` until it returns something truthy or the budget runs out.

    SQLite has no change notification, so this is a server-side poll — but the
    caller gets push-like latency from a single held connection instead of
    hammering the endpoint. `wait=0` degrades to exactly one call, which is
    what a background cron wants.
    """
    result = await run_in_threadpool(fetch)
    if result or wait <= 0:
        return result

    deadline = time.monotonic() + min(wait, MAX_WAIT_SECONDS)
    while time.monotonic() < deadline:
        # Stop burning cycles the moment the caller hangs up.
        if await request.is_disconnected():
            return result
        remaining = deadline - time.monotonic()
        await asyncio.sleep(min(_POLL_INTERVAL, max(0.0, remaining)))
        result = await run_in_threadpool(fetch)
        if result:
            return result
    return result

COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorOut, "description": "Malformed request"},
    401: {"model": ErrorOut, "description": "Missing or invalid credentials"},
    403: {"model": ErrorOut, "description": "Credential lacks the required scope"},
    404: {"model": ErrorOut, "description": "No such resource for this client"},
    413: {"model": ErrorOut, "description": "Request body or attachments too large"},
    422: {"model": ErrorOut, "description": "Field-level validation failure"},
    429: {
        "model": ErrorOut,
        "description": "Rate limit exceeded; see the Retry-After header",
        "headers": {
            "Retry-After": {"schema": {"type": "integer"}, "description": "Seconds to wait"},
        },
    },
}


# --------------------------------------------------------------------- meta


@router.get("/whoami", response_model=WhoAmI, responses=COMMON_ERRORS, tags=["meta"])
def whoami(principal: Principal = Depends(get_principal)) -> WhoAmI:
    """Verify a credential. Use this behind a 'Test connection' button."""
    return WhoAmI(
        client_id=principal.client_id,
        client_slug=principal.client_slug,
        client_name=principal.client_name,
        api_key_id=principal.api_key_id,
        api_key_name=principal.api_key_name,
        auth_type=principal.auth_type,
        server_time=now(),
    )


# ------------------------------------------------------------- upload tokens


@router.post(
    "/upload-tokens",
    response_model=UploadTokenOut,
    responses=COMMON_ERRORS,
    tags=["tickets"],
    status_code=201,
)
def create_upload_token(
    body: UploadTokenRequest,
    principal: Principal = Depends(require_api_key),
    store: Store = Depends(get_store),
) -> UploadTokenOut:
    """Mint a single-use token so a browser can upload without the API key.

    Call this from the client's Odoo backend, where the API key lives, and hand
    the returned token to the browser. The reporter identity is baked into the
    token, so the browser cannot file a ticket under anyone else's name.
    """
    token, expires = store.create_upload_token(
        principal, body.reporter.model_dump(), body.ttl_seconds
    )
    return UploadTokenOut(
        token=token,
        expires_at=expires,
        max_file_bytes=store.settings.max_file_bytes,
        max_files=store.settings.max_files_per_ticket,
    )


# ------------------------------------------------------------------- tickets


async def _parse_ticket_request(
    request: Request, store: Store
) -> tuple[TicketCreate, list[IncomingFile]]:
    """Read a ticket from either a JSON body or a multipart form."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    max_body = store.settings.max_body_bytes

    # Reject on the declared length before reading the socket, so a 25 MB
    # upload over a slow link fails immediately instead of after the transfer.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_body:
        raise ServiceError(
            "payload_too_large",
            f"request body is {declared} bytes, over the {max_body} byte limit",
            413,
        )

    if content_type == "multipart/form-data":
        form = await request.form()
        raw_payload = form.get("payload")
        if raw_payload is None:
            raise ServiceError("missing_payload", "multipart requests need a 'payload' field")
        try:
            data = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise ServiceError("invalid_json", f"'payload' is not valid JSON: {exc}") from exc
        try:
            payload = TicketCreate.model_validate(data)
        except ValidationError as exc:
            raise ServiceError(
                "validation_error", "payload failed validation", 422, detail=exc.errors()
            ) from exc

        files: list[IncomingFile] = []
        screenshot = form.get("screenshot")
        if screenshot is not None and hasattr(screenshot, "read"):
            files.append(
                IncomingFile(
                    data=await screenshot.read(),
                    filename=screenshot.filename or "screenshot.png",
                    content_type=screenshot.content_type or "image/png",
                    role="screenshot",
                )
            )
        for item in form.getlist("attachments"):
            if hasattr(item, "read"):
                files.append(
                    IncomingFile(
                        data=await item.read(),
                        filename=item.filename or "attachment",
                        content_type=item.content_type or "application/octet-stream",
                        role="attachment",
                    )
                )
        return payload, files

    body = await request.body()
    if len(body) > max_body:
        raise ServiceError(
            "payload_too_large",
            f"request body is {len(body)} bytes, over the {max_body} byte limit",
            413,
        )
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise ServiceError("invalid_json", f"body is not valid JSON: {exc}") from exc
    try:
        payload = TicketCreate.model_validate(data)
    except ValidationError as exc:
        raise ServiceError(
            "validation_error", "body failed validation", 422, detail=exc.errors()
        ) from exc

    files = []
    if payload.screenshot:
        files.append(incoming_from_inline(payload.screenshot, role="screenshot"))
    files.extend(incoming_from_inline(a) for a in payload.attachments)
    return payload, files


@router.post(
    "/tickets",
    response_model=TicketOut,
    status_code=201,
    responses=COMMON_ERRORS,
    tags=["tickets"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/TicketCreate"}},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["payload"],
                        "properties": {
                            "payload": {
                                "type": "string",
                                "description": "JSON-encoded TicketCreate (without inline files)",
                            },
                            "screenshot": {"type": "string", "format": "binary"},
                            "attachments": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                            },
                        },
                    }
                },
            },
        }
    },
)
async def create_ticket(
    request: Request,
    principal: Principal = Depends(get_principal),
    store: Store = Depends(get_store),
) -> TicketOut:
    """File a new ticket.

    Accepts an API key (the reporter must be supplied in the body) or a
    single-use upload token (the reporter is taken from the token).

    Send an `Idempotency-Key` header to make retries safe: a repeat with the
    same key returns the ticket created the first time instead of a duplicate.
    """
    payload, files = await _parse_ticket_request(request, store)
    idempotency_key = request.headers.get("idempotency-key")

    ticket = store.create_ticket(
        principal,
        title=payload.title,
        description=payload.description,
        reporter=payload.reporter.model_dump() if payload.reporter else None,
        priority=payload.priority,
        category=payload.category,
        tags=payload.tags,
        external_ref=payload.external_ref,
        context=payload.context.model_dump(mode="json", exclude_none=True),
        files=files,
        idempotency_key=idempotency_key,
    )
    return ticket_out(ticket)


@router.get("/tickets", response_model=TicketList, responses=COMMON_ERRORS, tags=["tickets"])
async def list_tickets(
    request: Request,
    status: Annotated[list[str] | None, Query(description="Repeatable status filter")] = None,
    updated_since: Annotated[datetime | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    reporter_email: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    wait: Annotated[
        int,
        Query(
            ge=0,
            le=MAX_WAIT_SECONDS,
            description="Hold the request up to this many seconds waiting for a change."
            " 0 (default) answers immediately.",
        ),
    ] = 0,
    principal: Principal = Depends(require_api_key),
    store: Store = Depends(get_store),
) -> TicketList:
    """List this client's tickets.

    Two modes, and the ordering differs because the jobs differ:

    * **Inbox** (no `updated_since`): newest first by `created_at`.
    * **Sync** (`updated_since`, or a cursor from a sync response): oldest
      change first by `(updated_at, id)`. Use this to poll for replies. Persist
      `next_cursor` and pass it back as `cursor`; it is always returned in this
      mode, so partial reads resume exactly where they stopped and you never
      have to reason about timestamps. Do not mix a cursor from one mode into
      the other — it is rejected rather than silently skipping rows.
    """
    def fetch():
        tickets, next_cursor, has_more = store.list_tickets(
            TicketFilters(
                client_id=principal.client_id,
                statuses=status,
                search=search,
                updated_since=updated_since,
                reporter_email=reporter_email,
                limit=limit,
                cursor=cursor,
            )
        )
        # Falsy when empty, which is what tells the long poll to keep waiting.
        return (tickets, next_cursor, has_more) if tickets else None

    result = await _long_poll(fetch, wait, request)
    if result is None:
        # Timed out with nothing new: re-read once so the caller still gets a
        # cursor to resume from rather than a null.
        tickets, next_cursor, has_more = await run_in_threadpool(
            lambda: store.list_tickets(
                TicketFilters(
                    client_id=principal.client_id,
                    statuses=status,
                    search=search,
                    updated_since=updated_since,
                    reporter_email=reporter_email,
                    limit=limit,
                    cursor=cursor,
                )
            )
        )
    else:
        tickets, next_cursor, has_more = result

    return TicketList(
        items=[ticket_out(t, include_comments=False) for t in tickets],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/tickets/{ticket_id}", response_model=TicketOut, responses=COMMON_ERRORS, tags=["tickets"]
)
def get_ticket(
    ticket_id: str,
    principal: Principal = Depends(require_api_key),
    store: Store = Depends(get_store),
) -> TicketOut:
    """Fetch one ticket by id or by human reference (e.g. `ACME-0042`).

    Internal operator notes are never included.
    """
    ticket = store.get_ticket(ticket_id, client_id=principal.client_id, include_internal=False)
    return ticket_out(ticket)


@router.patch(
    "/tickets/{ticket_id}", response_model=TicketOut, responses=COMMON_ERRORS, tags=["tickets"]
)
def update_ticket(
    ticket_id: str,
    body: TicketUpdate,
    principal: Principal = Depends(require_api_key),
    store: Store = Depends(get_store),
) -> TicketOut:
    """Edit a ticket. Clients may reopen or close, but not mark resolved."""
    changes = body.model_dump(exclude_unset=True, exclude_none=True, mode="json")
    ticket = store.update_ticket(
        ticket_id,
        changes,
        client_id=principal.client_id,
        actor=f"client:{principal.client_slug}",
    )
    return ticket_out(ticket)


# ------------------------------------------------------------------ comments


@router.get(
    "/tickets/{ticket_id}/comments",
    response_model=CommentList,
    responses=COMMON_ERRORS,
    tags=["comments"],
)
async def list_comments(
    request: Request,
    ticket_id: str,
    since: Annotated[datetime | None, Query(description="Only comments created after this")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    wait: Annotated[
        int,
        Query(
            ge=0,
            le=MAX_WAIT_SECONDS,
            description="Hold the request up to this many seconds waiting for a new"
            " comment. Use with `since` for a live conversation; 0 (default) answers"
            " immediately.",
        ),
    ] = 0,
    principal: Principal = Depends(require_api_key),
    store: Store = Depends(get_store),
) -> CommentList:
    """Comments oldest-first.

    Two modes from one endpoint. `since` alone is ordinary polling, right for a
    background job. Adding `wait` holds the connection until a reply lands, so
    a conversation someone is actually watching updates the moment the operator
    hits send, without hammering the server.
    """
    ticket = await run_in_threadpool(
        lambda: store.get_ticket(ticket_id, client_id=principal.client_id, comment_limit=None)
    )

    def fetch():
        found = store.list_comments(ticket.id, limit=limit, since=since)
        return found or None

    items = await _long_poll(fetch, wait, request) or []
    total = await run_in_threadpool(lambda: store.count_comments(ticket.id))
    return CommentList(
        items=[comment_out(c) for c in items], total=total, has_more=len(items) < total
    )


@router.post(
    "/tickets/{ticket_id}/comments",
    response_model=CommentOut,
    status_code=201,
    responses=COMMON_ERRORS,
    tags=["comments"],
)
def add_comment(
    ticket_id: str,
    body: CommentCreate,
    request: Request,
    principal: Principal = Depends(require_api_key),
    store: Store = Depends(get_store),
) -> CommentOut:
    """Add a follow-up from the client side, optionally with attachments.

    Honours `Idempotency-Key` the same way ticket creation does, scoped to
    this ticket: a retry returns the original comment rather than posting the
    same reply twice.
    """
    author_name = body.author.name if body.author else principal.client_name
    files = [incoming_from_inline(a) for a in body.attachments]
    comment = store.add_comment(
        ticket_id,
        body=body.body,
        author_type="client",
        author_name=author_name,
        visibility="public",
        files=files,
        client_id=principal.client_id,
        idempotency_key=request.headers.get("idempotency-key"),
    )
    return comment_out(comment)


# --------------------------------------------------------------- attachments


@router.post(
    "/tickets/{ticket_id}/attachments",
    response_model=list[dict],
    status_code=201,
    responses=COMMON_ERRORS,
    tags=["attachments"],
)
def add_attachments(
    ticket_id: str,
    body: list[InlineFile],
    principal: Principal = Depends(require_api_key),
    store: Store = Depends(get_store),
) -> list[dict]:
    """Attach more files to an existing ticket."""
    files = [incoming_from_inline(item) for item in body]
    stored = store.add_attachments(ticket_id, files, client_id=principal.client_id)
    return [attachment_out(a).model_dump(mode="json") for a in stored]


@router.get(
    "/attachments/{attachment_id}",
    responses={**COMMON_ERRORS, 200: {"content": {"application/octet-stream": {}}}},
    tags=["attachments"],
)
def download_attachment(
    attachment_id: str,
    principal: Principal = Depends(require_api_key),
    store: Store = Depends(get_store),
) -> Response:
    """Download a file. Scoped to the calling client's own tickets."""
    record, blob = store.get_attachment(attachment_id, client_id=principal.client_id)
    return Response(
        content=blob,
        media_type=record.content_type,
        headers={
            # `attachment` rather than `inline`: never let a client-supplied
            # file render in a browser origin we control.
            "Content-Disposition": f'attachment; filename="{record.filename}"',
            "Content-Length": str(record.size_bytes),
            "X-Content-Type-Options": "nosniff",
        },
    )
