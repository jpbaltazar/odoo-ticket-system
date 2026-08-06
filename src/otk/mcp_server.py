"""MCP server for triaging tickets.

Exposes the ticket store to an assistant so it can read a ticket — including
its screenshot — and leave findings behind as an internal note.

**It cannot talk to a client.** There is deliberately no tool that writes a
public comment, closes a ticket, or deletes anything. Every note it writes is
`visibility="internal"`, which no client-facing endpoint ever returns, and is
signed so a human can tell at a glance that a machine wrote it. A wrong or
hallucinated internal note costs a moment's confusion; the same note sent to a
client in the operator's name is a different kind of problem entirely.

Run it over SSH, so the database stays on the server:

    "otk-tickets": {
      "command": "ssh",
      "args": ["root@your-server", "/opt/odoo-tickets/.venv/bin/otk", "mcp"]
    }
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .config import Settings, get_settings
from .notify import PRIORITY_ORDER
from .service import ServiceError, Store, TicketFilters

# Signature on notes this server writes, so a human scanning a thread can tell
# machine triage from a colleague without reading the text. Over HTTP the
# operator behind the token is named too — the point of a per-operator token is
# that "who triaged this" has an answer.
TRIAGE_AUTHOR = "triage (automated)"


def _triage_author(store: Store) -> str:
    """Who to sign a note as, resolved per call from the presented token."""
    token = get_access_token()
    if token is None:
        return TRIAGE_AUTHOR  # stdio: no token, no identity to attribute to
    operator = store.operator_for_mcp_key(token.client_id)
    return f"{operator} · triage" if operator else TRIAGE_AUTHOR

MAX_BODY_CHARS = 4000


class StoreTokenVerifier:
    """Verifies MCP bearer tokens against the database.

    Implements the SDK's TokenVerifier protocol. Tokens are hashed with the
    server pepper and revocable, so a leaked one is killed with
    `otk mcp-key revoke` rather than by rotating anything else.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        principal = self._store.verify_mcp_key(token)
        if principal is None:
            return None
        # client_id carries the key id so a tool can resolve the operator, and
        # subject carries the username so it shows up in any audit logging.
        return AccessToken(
            token=token,
            client_id=principal.session_id,
            subject=principal.username,
            scopes=["triage"],
        )


def _summarise(ticket: Any) -> dict[str, Any]:
    """A list row: enough to choose what to look at, not the whole ticket."""
    context = ticket.context or {}
    error = context.get("error") or {}
    return {
        "ref": ticket.ref,
        "id": ticket.id,
        "title": ticket.title,
        "client": ticket.client_name,
        "status": ticket.status,
        "priority": ticket.priority,
        "category": ticket.category,
        "reporter": ticket.reporter_name,
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "unread": ticket.unread,
        "comment_count": ticket.comment_count,
        "has_screenshot": any(a.role == "screenshot" for a in ticket.attachments),
        "error_name": error.get("name"),
        "model": context.get("model"),
        "tags": ticket.tags,
    }


def build_server(store: Store | None = None, *, authenticated: bool = False) -> MCPServer:
    """Build the server.

    `authenticated` is for HTTP mode, where the endpoint is reachable by
    anyone who finds it. Over stdio it is off: the transport is a pipe from a
    process the operator already started, so a token would protect nothing.
    """
    store = store or Store(get_settings())
    settings: Settings = store.settings

    auth_kwargs: dict[str, Any] = {}
    if authenticated:
        if not settings.mcp_url:
            raise ServiceError(
                "mcp_url_required",
                "set OTK_MCP_URL to the public URL of the MCP endpoint before"
                " serving it over HTTP",
            )
        auth_kwargs = {
            "token_verifier": StoreTokenVerifier(store),
            "auth": AuthSettings(
                issuer_url=settings.mcp_url,
                resource_server_url=settings.mcp_url,
                required_scopes=["triage"],
            ),
        }

    server = MCPServer(
        name="odoo-tickets",
        **auth_kwargs,
        instructions=(
            "Triage support tickets from Odoo users. Read tickets, look at the "
            "screenshots, and record what you find as an INTERNAL note for the "
            "operator.\n\n"
            "You cannot reply to clients and must not try: notes you write are "
            "never shown to them. Write for the operator — what the error looks "
            "like, whether it resembles an earlier ticket, what is missing from "
            "the report, what you would check first. Say when you are unsure "
            "rather than guessing; a confident wrong diagnosis costs more time "
            "than no diagnosis."
        ),
    )

    read_only = ToolAnnotations(readOnlyHint=True)
    annotating = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

    # ------------------------------------------------------------------ read

    @server.tool(annotations=read_only)
    def list_tickets(
        status: str | None = None,
        client: str | None = None,
        unread_only: bool = False,
        search: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List tickets, newest first.

        `status` is one of new, open, waiting_client, waiting_third_party,
        resolved, closed. `client` is a client slug such as "abansec".
        """
        client_id = None
        if client:
            client_id = store.get_client_by_slug(client).id
        tickets, _, _ = store.list_tickets(
            TicketFilters(
                client_id=client_id,
                statuses=[status] if status else None,
                search=search,
                unread_only=unread_only,
                include_internal=True,
                limit=max(1, min(limit, 100)),
            )
        )
        return [_summarise(t) for t in tickets]

    @server.tool(annotations=read_only)
    def get_ticket(ref: str) -> dict[str, Any]:
        """Everything on one ticket, by reference (ABANSEC-0007) or id.

        Includes the full Odoo page context, any error and traceback, and the
        conversation — internal notes included, since you are triaging for the
        operator rather than for the client.
        """
        ticket = store.get_ticket(ref, include_internal=True, comment_limit=None)
        return {
            **_summarise(ticket),
            "description": ticket.description,
            "reporter_email": ticket.reporter_email,
            "context": ticket.context,
            "attachments": [
                {
                    "id": a.id,
                    "role": a.role,
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size_bytes": a.size_bytes,
                }
                for a in ticket.attachments
            ],
            "comments": [
                {
                    "author": c.author_name,
                    "author_type": c.author_type,
                    "internal": c.visibility == "internal",
                    "created_at": c.created_at.isoformat(),
                    "body": c.body,
                }
                for c in ticket.comments
            ],
            "purged": bool(ticket.purged_at),
        }

    @server.tool(annotations=read_only)
    def get_screenshot(ref: str) -> Image:
        """The screenshot attached to a ticket, so you can look at it.

        Usually the most informative part of a report — the error dialog, the
        state of the form, which fields were filled.
        """
        ticket = store.get_ticket(ref)
        shots = [a for a in ticket.attachments if a.role == "screenshot"]
        images = shots or [a for a in ticket.attachments if a.content_type.startswith("image/")]
        if not images:
            raise ServiceError("no_screenshot", f"{ticket.ref} has no image attached")
        record, blob = store.get_attachment(images[0].id)
        return Image(data=blob, format=record.content_type.split("/")[-1])

    @server.tool(annotations=read_only)
    def find_similar(ref: str, limit: int = 5) -> list[dict[str, Any]]:
        """Other tickets that look like this one.

        Matches on the same Odoo model or the same error name — the two things
        that actually recur. Use it to spot a ticket you have already answered.
        """
        ticket = store.get_ticket(ref)
        context = ticket.context or {}
        model = context.get("model")
        error_name = ((context.get("error") or {}).get("name")) or None

        candidates, _, _ = store.list_tickets(
            TicketFilters(include_internal=True, limit=200)
        )
        matches = []
        for other in candidates:
            if other.id == ticket.id:
                continue
            other_ctx = other.context or {}
            other_error = (other_ctx.get("error") or {}).get("name")
            reasons = []
            if model and other_ctx.get("model") == model:
                reasons.append(f"same model ({model})")
            if error_name and other_error == error_name:
                reasons.append(f"same error ({error_name})")
            if reasons:
                matches.append({**_summarise(other), "why": ", ".join(reasons)})
        return matches[: max(1, min(limit, 25))]

    @server.tool(annotations=read_only)
    def list_clients() -> list[dict[str, Any]]:
        """Clients, with their open ticket counts."""
        result = []
        for client in store.list_clients():
            counts = store.counts_by_status(client.id)
            result.append(
                {
                    "slug": client.slug,
                    "name": client.name,
                    "active": client.active,
                    "tickets": sum(counts.values()),
                    "by_status": counts,
                }
            )
        return result

    # -------------------------------------------------------------- annotate

    @server.tool(annotations=annotating)
    def add_internal_note(ref: str, body: str) -> dict[str, Any]:
        """Record a triage finding on a ticket.

        The note is internal: the client never sees it, on this or any other
        endpoint. Write it for the operator. Say what you are unsure about
        rather than smoothing over it.
        """
        body = body.strip()
        if not body:
            raise ServiceError("empty_note", "a note needs a body")
        author = _triage_author(store)
        comment = store.add_comment(
            ref,
            body=body[:MAX_BODY_CHARS],
            author_type="agent",
            author_name=author,
            visibility="internal",
        )
        return {
            "comment_id": comment.id,
            "visibility": "internal",
            "ticket": ref,
            "signed_as": author,
        }

    @server.tool(annotations=annotating)
    def suggest_priority(ref: str, priority: str, because: str) -> dict[str, Any]:
        """Change a ticket's priority, recording why in an internal note.

        `priority` is low, normal, high or urgent. The reason is required and
        is stored — a priority that changed with no explanation is worse than
        one that never changed.
        """
        if priority not in PRIORITY_ORDER:
            raise ServiceError(
                "invalid_priority", f"priority must be one of {', '.join(PRIORITY_ORDER)}"
            )
        ticket = store.get_ticket(ref)
        before = ticket.priority
        author = _triage_author(store)
        store.update_ticket(ticket.id, {"priority": priority}, actor=author)
        store.add_comment(
            ticket.id,
            body=f"Priority {before} → {priority}. {because.strip()}"[:MAX_BODY_CHARS],
            author_type="agent",
            author_name=author,
            visibility="internal",
        )
        return {
            "ticket": ticket.ref,
            "priority_was": before,
            "priority_now": priority,
            "signed_as": author,
        }

    @server.tool(annotations=annotating)
    def add_tags(ref: str, tags: list[str]) -> dict[str, Any]:
        """Add tags to a ticket, keeping the ones already on it."""
        ticket = store.get_ticket(ref)
        merged = sorted({*ticket.tags, *(t.strip() for t in tags if t.strip())})
        store.update_ticket(ticket.id, {"tags": merged}, actor=_triage_author(store))
        return {"ticket": ticket.ref, "tags": merged}

    return server


def run() -> None:
    """Serve on stdio, for a locally launched client."""
    build_server().run(transport="stdio")


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """Allow the public hostname through the DNS-rebinding check.

    The SDK validates Host and Origin to stop a browser on a victim's machine
    being tricked into driving a locally bound MCP server. Behind a reverse
    proxy the Host is the public name, which is not in the default allowlist —
    so without this every proxied request is refused with
    `421 Invalid Host header`, whatever the token says.

    Only the configured hostname is added, so the protection is narrowed to
    this deployment rather than switched off.
    """
    parsed = urlparse(settings.mcp_url)
    host = parsed.netloc or parsed.path
    origin = f"{parsed.scheme}://{host}" if parsed.scheme else f"https://{host}"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        # Both bare and :port forms: a proxy may or may not pass the port on.
        allowed_hosts=[host, f"{host}:80", f"{host}:443"],
        allowed_origins=[origin],
    )


def run_http(host: str | None = None, port: int | None = None) -> None:
    """Serve over Streamable HTTP, for a client that connects over the network.

    Always authenticated: unlike stdio, anyone who reaches the URL can try.
    """
    import uvicorn

    store = Store(get_settings())
    settings = store.settings
    server = build_server(store, authenticated=True)
    app = server.streamable_http_app(
        transport_security=_transport_security(settings),
    )
    uvicorn.run(
        app,
        host=host or settings.host,
        port=port or settings.mcp_port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
