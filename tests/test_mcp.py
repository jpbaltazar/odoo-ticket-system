"""The triage MCP server.

The tools are exercised through the server's own dispatch rather than by
calling the Python functions directly, so the registered schema and the
handler are both covered.
"""

from __future__ import annotations

import io
import json

import pytest

from otk.service import IncomingFile, Principal, Store

pytest.importorskip("mcp", reason="MCP SDK is an optional extra")

from otk.mcp_server import TRIAGE_AUTHOR, build_server  # noqa: E402


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def seeded(store: Store):
    client = store.create_client(name="Abansec", slug="abansec")
    principal = Principal(
        auth_type="api_key",
        client_id=client.id,
        client_slug=client.slug,
        client_name=client.name,
        api_key_id="k",
        api_key_name="k",
    )
    context = {
        "url": "https://www.abansec.com/odoo/sales/12043",
        "odoo_version": "19.0+e",
        "model": "sale.order",
        "res_id": 12043,
        "error": {"name": "UserError", "message": "Insufficient stock"},
    }
    first = store.create_ticket(
        principal,
        title="Cannot confirm sale order",
        reporter={"name": "Marta"},
        context=context,
        priority="normal",
        files=[
            IncomingFile(
                data=_png(), filename="s.png", content_type="image/png", role="screenshot"
            )
        ],
    )
    # A second ticket on the same model and error, for find_similar.
    store.create_ticket(
        principal, title="Same thing again", reporter={"name": "João"}, context=context
    )
    return store, first


@pytest.fixture
def server(seeded):
    store, _ = seeded
    return build_server(store)


async def _call(server, name: str, **arguments):
    """Invoke a tool the way a client would, and unwrap the result.

    SDK 2.0 returns a CallToolResult and reports failures as `is_error` rather
    than raising, so a test that only looked at the content would pass on an
    error string.
    """
    result = await server.call_tool(name, arguments)
    if result.is_error:
        raise RuntimeError(result.content[0].text)
    if result.structured_content:
        payload = result.structured_content
        return payload.get("result", payload) if isinstance(payload, dict) else payload
    return json.loads(result.content[0].text)


# ------------------------------------------------------------------ surface


@pytest.mark.anyio
async def test_no_tool_can_reach_a_client(server):
    """The whole safety story in one assertion.

    Nothing here may post a public comment, close a ticket or delete anything.
    A wrong internal note costs a moment; the same text sent to a client in the
    operator's name is a different kind of problem.
    """
    names = {tool.name for tool in await server.list_tools()}
    assert names == {
        "list_tickets",
        "get_ticket",
        "get_screenshot",
        "find_similar",
        "list_clients",
        "add_internal_note",
        "suggest_priority",
        "add_tags",
    }
    forbidden = {"reply", "comment", "close", "resolve", "delete", "purge", "email", "send"}
    for name in names:
        assert not (forbidden & set(name.split("_"))), f"{name} looks client-facing"


@pytest.mark.anyio
async def test_read_tools_are_marked_read_only(server):
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in ("list_tickets", "get_ticket", "get_screenshot", "find_similar"):
        assert tools[name].annotations.read_only_hint is True


# --------------------------------------------------------------------- read


@pytest.mark.anyio
async def test_list_and_get(server, seeded):
    _, ticket = seeded
    rows = await _call(server, "list_tickets", limit=10)
    assert any(r["ref"] == ticket.ref for r in rows)

    detail = await _call(server, "get_ticket", ref=ticket.ref)
    assert detail["title"] == "Cannot confirm sale order"
    assert detail["context"]["model"] == "sale.order"
    assert detail["context"]["error"]["name"] == "UserError"
    assert detail["has_screenshot"] is True


@pytest.mark.anyio
async def test_internal_notes_are_visible_to_triage(server, seeded):
    """It triages for the operator, so it must see the operator's own notes."""
    store, ticket = seeded
    store.add_comment(
        ticket.id,
        body="cheap support plan",
        author_type="agent",
        author_name="José",
        visibility="internal",
    )
    detail = await _call(server, "get_ticket", ref=ticket.ref)
    assert [c["internal"] for c in detail["comments"]] == [True]


@pytest.mark.anyio
async def test_get_screenshot_returns_the_image(server, seeded):
    _, ticket = seeded
    result = await server.call_tool("get_screenshot", {"ref": ticket.ref})
    assert result.is_error is not True
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/png"


@pytest.mark.anyio
async def test_find_similar_matches_model_and_error(server, seeded):
    _, ticket = seeded
    matches = await _call(server, "find_similar", ref=ticket.ref)
    assert len(matches) == 1
    assert "sale.order" in matches[0]["why"]
    assert "UserError" in matches[0]["why"]


# ----------------------------------------------------------------- annotate


@pytest.mark.anyio
async def test_notes_are_internal_and_signed(server, seeded):
    store, ticket = seeded
    await _call(server, "add_internal_note", ref=ticket.ref, body="Looks like a stock rule.")

    comments = store.get_ticket(ticket.id, include_internal=True).comments
    assert [(c.visibility, c.author_name) for c in comments] == [("internal", TRIAGE_AUTHOR)]

    # And the client sees nothing at all.
    assert store.get_ticket(ticket.id, include_internal=False).comments == []


@pytest.mark.anyio
async def test_priority_change_records_its_reason(server, seeded):
    store, ticket = seeded
    await _call(
        server,
        "suggest_priority",
        ref=ticket.ref,
        priority="high",
        because="Blocks invoicing for the month.",
    )
    updated = store.get_ticket(ticket.id, include_internal=True)
    assert updated.priority == "high"
    note = updated.comments[-1]
    assert note.visibility == "internal"
    assert "normal → high" in note.body
    assert "Blocks invoicing" in note.body


@pytest.mark.anyio
async def test_tags_merge_rather_than_replace(server, seeded):
    store, ticket = seeded
    store.update_ticket(ticket.id, {"tags": ["existing"]}, actor="operator")
    await _call(server, "add_tags", ref=ticket.ref, tags=["stock", "duplicate"])
    assert store.get_ticket(ticket.id).tags == ["duplicate", "existing", "stock"]


@pytest.mark.anyio
async def test_an_empty_note_is_refused(server, seeded):
    _, ticket = seeded
    # The SDK turns an exception in a tool into ToolError rather than an
    # is_error result, so this is the shape a client actually sees.
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="a note needs a body"):
        await _call(server, "add_internal_note", ref=ticket.ref, body="   ")
