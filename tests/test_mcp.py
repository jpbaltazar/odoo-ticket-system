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


# ------------------------------------------------------------ bearer tokens


@pytest.fixture
def operator(store: Store) -> str:
    store.create_operator("jose", "correct-horse-battery-staple", "José Mendes")
    return "jose"


def test_token_round_trip_identifies_the_operator(store, operator):
    _, token = store.issue_mcp_key(operator, name="claude-web")
    principal = store.verify_mcp_key(token)
    assert principal is not None
    assert principal.username == "jose"
    assert principal.display_name == "José Mendes"


def test_tokens_are_per_operator(store, operator):
    store.create_operator("ana", "another-long-password-1", "Ana Costa")
    _, jose_token = store.issue_mcp_key("jose")
    _, ana_token = store.issue_mcp_key("ana")
    assert store.verify_mcp_key(jose_token).username == "jose"
    assert store.verify_mcp_key(ana_token).username == "ana"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-token",
        "otm_test_aaaaaaaaaa_bbbbbbbbbb",  # well-formed, unknown id
    ],
)
def test_bad_tokens_are_rejected(store, operator, bad):
    assert store.verify_mcp_key(bad) is None


def test_wrong_secret_for_a_real_key_is_rejected(store, operator):
    _, token = store.issue_mcp_key(operator)
    prefix, env, key_id, _ = token.split("_")
    assert store.verify_mcp_key(f"{prefix}_{env}_{key_id}_{'z' * 52}") is None


def test_revoked_token_stops_working(store, operator):
    key_id, token = store.issue_mcp_key(operator)
    assert store.verify_mcp_key(token) is not None
    store.revoke_mcp_key(key_id)
    assert store.verify_mcp_key(token) is None


def test_removing_an_operator_kills_their_tokens(store, operator):
    store.create_operator("temp", "another-long-password-1")
    _, token = store.issue_mcp_key("temp")
    assert store.verify_mcp_key(token) is not None
    store.delete_operator("temp")
    assert store.verify_mcp_key(token) is None, "cascade must take the token with it"


def test_other_credential_types_are_not_accepted(store, operator):
    """An API key or a web session must not open the MCP endpoint."""
    client = store.create_client(name="Acme", slug="acme-2")
    _, api_key = store.issue_api_key(client.id, name="prod")
    session, _ = store.login("jose", "correct-horse-battery-staple")
    assert store.verify_mcp_key(api_key) is None
    assert store.verify_mcp_key(session) is None


def test_mcp_token_is_not_accepted_as_an_api_key(settings, store, operator):
    from fastapi.testclient import TestClient

    from otk.api.app import create_app

    _, token = store.issue_mcp_key(operator)
    with TestClient(create_app(settings, store)) as api:
        response = api.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_http_mode_refuses_to_start_without_a_public_url(store, operator):
    """The auth spec advertises the resource URL; a wrong one makes tokens
    issued for one host usable against another."""
    from otk.service import ServiceError

    with pytest.raises(ServiceError, match="OTK_MCP_URL"):
        build_server(store, authenticated=True)


def test_http_mode_builds_with_a_public_url(settings, store, operator):
    from dataclasses import replace

    configured = Store(replace(settings, mcp_url="https://mcp.example.com"))
    configured.create_operator("jose2", "correct-horse-battery-staple", "J")
    assert build_server(configured, authenticated=True) is not None


@pytest.mark.anyio
async def test_notes_are_signed_with_the_operator_when_authenticated(
    seeded, operator, monkeypatch
):
    """A per-operator token exists so 'who triaged this' has an answer."""
    import otk.mcp_server as mod

    store_obj, ticket = seeded
    key_id, _ = store_obj.issue_mcp_key(operator)

    class _Token:
        client_id = key_id

    monkeypatch.setattr(mod, "get_access_token", lambda: _Token())
    server = build_server(store_obj)
    await server.call_tool("add_internal_note", {"ref": ticket.ref, "body": "Looks like a dup."})

    note = store_obj.get_ticket(ticket.id, include_internal=True).comments[-1]
    assert note.author_name == "José Mendes · triage"
    assert note.visibility == "internal"


@pytest.mark.anyio
async def test_notes_fall_back_to_the_generic_signature_over_stdio(seeded):
    """No token means no identity to attribute to, not a crash."""
    store_obj, ticket = seeded
    server = build_server(store_obj)
    await server.call_tool("add_internal_note", {"ref": ticket.ref, "body": "note"})
    assert store_obj.get_ticket(ticket.id, include_internal=True).comments[-1].author_name == (
        TRIAGE_AUTHOR
    )


# ------------------------------------------------------- DNS-rebinding guard


def test_public_hostname_is_allowed_through_the_host_check(settings):
    """Behind a proxy the Host is the public name, not 127.0.0.1. Without it
    in the allowlist every proxied request is refused `421 Invalid Host
    header`, whatever the token says."""
    from dataclasses import replace

    from otk.mcp_server import _transport_security

    configured = replace(settings, mcp_url="https://tickets.admin.abansec.com/mcp")
    security = _transport_security(configured)

    assert security.enable_dns_rebinding_protection is True
    assert "tickets.admin.abansec.com" in security.allowed_hosts
    # A proxy may or may not pass the port through.
    assert "tickets.admin.abansec.com:443" in security.allowed_hosts
    assert security.allowed_origins == ["https://tickets.admin.abansec.com"]


def test_only_the_configured_host_is_allowed(settings):
    """Narrowed to this deployment rather than switched off."""
    from dataclasses import replace

    from otk.mcp_server import _transport_security

    security = _transport_security(replace(settings, mcp_url="https://mine.example.com/mcp"))
    assert not any("evil" in h for h in security.allowed_hosts)
    assert all(h.startswith("mine.example.com") for h in security.allowed_hosts)
