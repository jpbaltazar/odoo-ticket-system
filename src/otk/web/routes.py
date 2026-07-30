"""Operator web pages.

Server-rendered HTML with a little vanilla JS. No build step, no bundler, and
no client-side framework: this is a handful of pages for one person, and the
deployment story matters more than the interaction model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import OperatorPrincipal
from ..schemas import Priority, TicketStatus
from ..service import ServiceError, Store, TicketFilters, now
from .deps import SESSION_COOKIE, require_csrf, require_operator, get_store

router = APIRouter()

STATUSES = [s.value for s in TicketStatus]
PRIORITIES = [p.value for p in Priority]


def _templates(request: Request):
    return request.app.state.templates


def _render(request: Request, name: str, **context) -> HTMLResponse:
    operator = getattr(request.state, "operator", None)
    store: Store = request.app.state.store
    base = {
        "operator": operator,
        "csrf_token": operator.csrf_token if operator else "",
        "unread_count": store.unread_count(),
        "nav_clients": store.list_clients(),
        "statuses": STATUSES,
        "priorities": PRIORITIES,
    }
    return _templates(request).TemplateResponse(
        request=request, name=name, context={**base, **context}
    )


# ------------------------------------------------------------------- login


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", store: Store = Depends(get_store)):
    return _templates(request).TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "next": next, "no_operators": not store.has_operators()},
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
    store: Store = Depends(get_store),
):
    # Throttle by username so a slow brute force against one account is
    # blocked without letting anyone lock out an operator from another IP.
    try:
        store.check_rate_limit(f"login:{username.strip().lower()}", per_minute=10)
        cookie, _ = store.login(username, password, request.headers.get("user-agent"))
    except ServiceError as exc:
        return _templates(request).TemplateResponse(
            request=request,
            name="login.html",
            context={"error": exc.message, "next": next, "no_operators": False},
            status_code=401,
        )

    # Only send the cookie over HTTPS when the request itself arrived over
    # HTTPS, otherwise a local http:// deployment could never sign in.
    secure = request.url.scheme == "https" or request.headers.get(
        "x-forwarded-proto"
    ) == "https"
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=14 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return response


@router.post("/logout")
def logout(
    request: Request,
    operator: OperatorPrincipal = Depends(require_csrf),
    store: Store = Depends(get_store),
):
    store.logout(operator.session_id)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ------------------------------------------------------------------- inbox


@router.get("/", response_class=HTMLResponse)
def inbox(
    request: Request,
    q: str | None = None,
    client: str | None = None,
    view: str = "open",
    operator: OperatorPrincipal = Depends(require_operator),
    store: Store = Depends(get_store),
):
    tickets, _, _ = store.list_tickets(
        TicketFilters(
            client_id=client or None,
            search=q or None,
            include_closed=view in ("all", "closed"),
            unread_only=view == "unread",
            include_internal=True,
            statuses=["closed", "resolved"] if view == "closed" else None,
            limit=200,
        )
    )
    return _render(
        request,
        "inbox.html",
        tickets=tickets,
        counts=store.counts_by_status(client or None),
        q=q or "",
        client=client or "",
        view=view,
    )


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(
    request: Request,
    ticket_id: str,
    operator: OperatorPrincipal = Depends(require_operator),
    store: Store = Depends(get_store),
):
    ticket = store.get_ticket(ticket_id, include_internal=True, comment_limit=None)
    store.mark_read(ticket.id)
    return _render(request, "ticket.html", ticket=ticket)


@router.post("/tickets/{ticket_id}/update")
async def ticket_update(
    request: Request,
    ticket_id: str,
    operator: OperatorPrincipal = Depends(require_csrf),
    store: Store = Depends(get_store),
):
    form = await request.form()
    changes = {
        key: str(form[key]).strip()
        for key in ("status", "priority", "assignee", "title")
        if form.get(key) is not None and str(form[key]).strip() != ""
    }
    if changes:
        store.update_ticket(ticket_id, changes, actor=f"operator:{operator.username}")
    return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)


@router.post("/tickets/{ticket_id}/reply")
async def ticket_reply(
    request: Request,
    ticket_id: str,
    operator: OperatorPrincipal = Depends(require_csrf),
    store: Store = Depends(get_store),
):
    form = await request.form()
    body = str(form.get("body", "")).strip()
    if body:
        store.add_comment(
            ticket_id,
            body=body,
            author_type="agent",
            author_name=operator.display_name,
            visibility="internal" if form.get("internal") else "public",
        )
    # Land back on the thread rather than the top of the page: after a long
    # screenshot and context block, scrolling back down every time is tedious.
    return RedirectResponse(f"/tickets/{ticket_id}#conversation", status_code=303)


@router.post("/tickets/{ticket_id}/unread")
def ticket_unread(
    ticket_id: str,
    operator: OperatorPrincipal = Depends(require_csrf),
    store: Store = Depends(get_store),
):
    store.mark_read(ticket_id, read=False)
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------- attachments


@router.get("/attachments/{attachment_id}")
def attachment(
    attachment_id: str,
    download: bool = False,
    operator: OperatorPrincipal = Depends(require_operator),
    store: Store = Depends(get_store),
) -> Response:
    """Serve a file to the signed-in operator, unscoped by client."""
    record, blob = store.get_attachment(attachment_id)
    # Render images and PDFs in the browser; force everything else to download.
    # A client-supplied XML or HTML-ish file rendered inline would execute in
    # this origin, which is the one place that actually matters.
    inline = record.content_type.startswith("image/") or record.content_type == "application/pdf"
    disposition = "attachment" if download or not inline else "inline"
    return Response(
        content=blob,
        media_type=record.content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{record.filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.get("/attachments/{attachment_id}/thumb")
def attachment_thumb(
    attachment_id: str,
    operator: OperatorPrincipal = Depends(require_operator),
    store: Store = Depends(get_store),
) -> Response:
    record, _ = store.get_attachment(attachment_id)
    thumb = store.blobs.thumbnail(record.sha256, record.content_type)
    if thumb is None:
        return Response(status_code=404)
    return Response(
        content=thumb,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=86400"},
    )


# -------------------------------------------------------------- retention


@router.get("/storage", response_class=HTMLResponse)
def storage_page(
    request: Request,
    days: Annotated[int, Query(ge=1, le=3650)] = 90,
    scope: str = "closed",
    operator: OperatorPrincipal = Depends(require_operator),
    store: Store = Depends(get_store),
):
    """Dry run: show exactly what a purge would remove before anything goes."""
    statuses = (
        [TicketStatus.CLOSED, TicketStatus.RESOLVED] if scope == "closed" else list(TicketStatus)
    )
    candidates = store.purge_candidates(older_than_days=days, statuses=statuses)
    return _render(
        request,
        "storage.html",
        candidates=candidates,
        days=days,
        scope=scope,
        usage=store.storage_usage(),
        orphans=store.collect_orphan_blobs(dry_run=True),
        total_bytes=sum(c["bytes"] for c in candidates),
        total_files=sum(c["file_count"] for c in candidates),
        retention_days=store.settings.retention_days,
    )


@router.post("/storage/purge")
async def storage_purge(
    request: Request,
    operator: OperatorPrincipal = Depends(require_csrf),
    store: Store = Depends(get_store),
):
    form = await request.form()
    ticket_ids = [str(v) for v in form.getlist("ticket_id")]
    if ticket_ids:
        result = store.purge_attachments(ticket_ids)
        store._log_event(
            None, None, f"operator:{operator.username}", "purge.manual", result
        )
    return RedirectResponse("/storage?purged=1", status_code=303)


@router.post("/storage/gc")
def storage_gc(
    operator: OperatorPrincipal = Depends(require_csrf),
    store: Store = Depends(get_store),
):
    store.collect_orphan_blobs(dry_run=False)
    return RedirectResponse("/storage", status_code=303)
