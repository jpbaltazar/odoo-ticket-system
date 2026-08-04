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

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.types import ToolAnnotations

from .config import get_settings
from .notify import PRIORITY_ORDER
from .service import ServiceError, Store, TicketFilters

# Notes this server writes are signed with this, so a human scanning a thread
# can tell machine triage from a colleague without reading the text.
TRIAGE_AUTHOR = "triage (automated)"

MAX_BODY_CHARS = 4000


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


def build_server(store: Store | None = None) -> MCPServer:
    store = store or Store(get_settings())
    server = MCPServer(
        name="odoo-tickets",
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
        comment = store.add_comment(
            ref,
            body=body[:MAX_BODY_CHARS],
            author_type="agent",
            author_name=TRIAGE_AUTHOR,
            visibility="internal",
        )
        return {"comment_id": comment.id, "visibility": "internal", "ticket": ref}

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
        store.update_ticket(ticket.id, {"priority": priority}, actor=TRIAGE_AUTHOR)
        store.add_comment(
            ticket.id,
            body=f"Priority {before} → {priority}. {because.strip()}"[:MAX_BODY_CHARS],
            author_type="agent",
            author_name=TRIAGE_AUTHOR,
            visibility="internal",
        )
        return {"ticket": ticket.ref, "priority_was": before, "priority_now": priority}

    @server.tool(annotations=annotating)
    def add_tags(ref: str, tags: list[str]) -> dict[str, Any]:
        """Add tags to a ticket, keeping the ones already on it."""
        ticket = store.get_ticket(ref)
        merged = sorted({*ticket.tags, *(t.strip() for t in tags if t.strip())})
        store.update_ticket(ticket.id, {"tags": merged}, actor=TRIAGE_AUTHOR)
        return {"ticket": ticket.ref, "tags": merged}

    return server


def run() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    run()
