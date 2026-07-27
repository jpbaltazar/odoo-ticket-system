"""End-to-end tests over the HTTP API, exercising both auth paths."""

from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient

from otk.api.app import create_app
from otk.config import Settings
from otk.service import Store


def _png(colour: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), colour).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def client_and_key(store):
    record = store.create_client(name="Acme Industries", slug="acme")
    _, token = store.issue_api_key(record.id, name="prod")
    return record, token


@pytest.fixture
def api(settings, store) -> TestClient:
    with TestClient(create_app(settings, store)) as test_client:
        yield test_client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------- auth


def test_health_needs_no_auth(api):
    assert api.get("/health").json()["status"] == "ok"


def test_whoami_identifies_the_client(api, client_and_key):
    record, token = client_and_key
    body = api.get("/api/v1/whoami", headers=auth(token)).json()
    assert body["client_slug"] == "acme"
    assert body["auth_type"] == "api_key"
    assert body["client_id"] == record.id


@pytest.mark.parametrize(
    "header,expected",
    [
        ({}, "missing_credentials"),
        ({"Authorization": "Bearer nonsense"}, "invalid_credentials"),
        ({"Authorization": "Bearer otk_test_aaaaaaaaaa_bbbbbbbbbb"}, "invalid_api_key"),
    ],
)
def test_bad_credentials_are_rejected(api, header, expected):
    response = api.get("/api/v1/whoami", headers=header)
    assert response.status_code == 401
    assert response.json()["error"] == expected


def test_wrong_secret_for_a_real_key_id_is_rejected(api, store, client_and_key):
    _, token = client_and_key
    prefix, env, key_id, _ = token.split("_")
    forged = f"{prefix}_{env}_{key_id}_{'z' * 52}"
    response = api.get("/api/v1/whoami", headers=auth(forged))
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_api_key"


def test_revoked_key_stops_working(api, store, client_and_key):
    record, token = client_and_key
    key_id = store.list_api_keys(record.id)[0]["id"]
    store.revoke_api_key(key_id)
    assert api.get("/api/v1/whoami", headers=auth(token)).json()["error"] == "revoked_api_key"


def test_disabled_client_is_locked_out(api, store, client_and_key):
    record, token = client_and_key
    store.set_client_active(record.id, False)
    response = api.get("/api/v1/whoami", headers=auth(token))
    assert response.status_code == 403


# ---------------------------------------------------------------- creation


MINIMAL = {"title": "Cannot confirm sale order", "reporter": {"name": "Marta Silva"}}


def test_create_ticket_json_with_screenshot(api, client_and_key):
    _, token = client_and_key
    payload = {
        **MINIMAL,
        "description": "Fails with a UserError.",
        "priority": "high",
        "category": "bug",
        "tags": ["sales", "stock"],
        "context": {
            "url": "https://acme.odoo.example/odoo/sales/12043",
            "odoo_version": "17.0",
            "database": "acme-prod",
            "model": "sale.order",
            "res_id": 12043,
            "error": {"name": "UserError", "message": "Insufficient stock"},
        },
        "screenshot": {
            "filename": "err.png",
            "content_type": "image/png",
            "data": base64.b64encode(_png()).decode(),
        },
    }
    response = api.post("/api/v1/tickets", json=payload, headers=auth(token))
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["ref"] == "ACME-0001"
    assert body["status"] == "new"
    assert body["source"] == "odoo_server"
    assert body["context"]["res_id"] == 12043
    assert body["context"]["error"]["name"] == "UserError"
    assert body["tags"] == ["sales", "stock"]

    screenshot = [a for a in body["attachments"] if a["role"] == "screenshot"]
    assert len(screenshot) == 1
    assert screenshot[0]["content_type"] == "image/png"
    assert (screenshot[0]["width"], screenshot[0]["height"]) == (48, 32)


def test_screenshot_accepts_a_data_uri(api, client_and_key):
    _, token = client_and_key
    payload = {
        **MINIMAL,
        "screenshot": {
            "filename": "err.png",
            "data": "data:image/png;base64," + base64.b64encode(_png()).decode(),
        },
    }
    body = api.post("/api/v1/tickets", json=payload, headers=auth(token)).json()
    assert body["attachments"][0]["content_type"] == "image/png"


def test_create_ticket_multipart(api, client_and_key):
    import json

    _, token = client_and_key
    response = api.post(
        "/api/v1/tickets",
        data={"payload": json.dumps({**MINIMAL, "priority": "urgent"})},
        files={"screenshot": ("shot.png", _png(), "image/png")},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["priority"] == "urgent"
    assert body["attachments"][0]["role"] == "screenshot"


def test_ticket_refs_increment_per_client(api, store, client_and_key):
    _, token = client_and_key
    other = store.create_client(name="Globex", slug="globex")
    _, other_token = store.issue_api_key(other.id, name="prod")

    assert api.post("/api/v1/tickets", json=MINIMAL, headers=auth(token)).json()["ref"] == "ACME-0001"
    assert api.post("/api/v1/tickets", json=MINIMAL, headers=auth(token)).json()["ref"] == "ACME-0002"
    assert (
        api.post("/api/v1/tickets", json=MINIMAL, headers=auth(other_token)).json()["ref"]
        == "GLOBEX-0001"
    )


def test_reporter_is_required_for_api_key_auth(api, client_and_key):
    _, token = client_and_key
    response = api.post("/api/v1/tickets", json={"title": "No reporter"}, headers=auth(token))
    assert response.status_code == 400
    assert response.json()["error"] == "reporter_required"


def test_idempotency_key_prevents_duplicates(api, client_and_key):
    _, token = client_and_key
    headers = {**auth(token), "Idempotency-Key": "abc-123"}
    first = api.post("/api/v1/tickets", json=MINIMAL, headers=headers).json()
    second = api.post("/api/v1/tickets", json=MINIMAL, headers=headers).json()
    assert first["id"] == second["id"]
    assert first["ref"] == second["ref"]


def test_executables_are_rejected(api, client_and_key):
    _, token = client_and_key
    payload = {
        **MINIMAL,
        "attachments": [
            {
                "filename": "evil.sh",
                "content_type": "application/x-sh",
                "data": base64.b64encode(b"#!/bin/sh\nrm -rf /\n").decode(),
            }
        ],
    }
    response = api.post("/api/v1/tickets", json=payload, headers=auth(token))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_attachment"


def test_content_type_must_match_the_bytes(api, client_and_key):
    _, token = client_and_key
    payload = {
        **MINIMAL,
        "screenshot": {
            "filename": "fake.png",
            "content_type": "image/png",
            "data": base64.b64encode(b"not actually a png").decode(),
        },
    }
    response = api.post("/api/v1/tickets", json=payload, headers=auth(token))
    assert response.status_code == 400


def test_oversized_attachment_is_rejected(api, client_and_key, settings):
    _, token = client_and_key
    huge = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * settings.max_file_bytes).decode()
    payload = {**MINIMAL, "screenshot": {"filename": "big.png", "content_type": "image/png", "data": huge}}
    response = api.post("/api/v1/tickets", json=payload, headers=auth(token))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_attachment"


# ------------------------------------------------------------ upload tokens


def test_upload_token_round_trip(api, client_and_key):
    _, token = client_and_key
    minted = api.post(
        "/api/v1/upload-tokens",
        json={"reporter": {"name": "Marta Silva", "odoo_uid": 7}},
        headers=auth(token),
    )
    assert minted.status_code == 201, minted.text
    upload_token = minted.json()["token"]
    assert upload_token.startswith("ott_")

    identity = api.get("/api/v1/whoami", headers=auth(upload_token)).json()
    assert identity["auth_type"] == "upload_token"

    created = api.post(
        "/api/v1/tickets",
        json={"title": "Filed from the browser"},
        headers=auth(upload_token),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["source"] == "odoo_browser"
    assert body["reporter"]["name"] == "Marta Silva"


def test_upload_token_reporter_cannot_be_spoofed(api, client_and_key):
    _, token = client_and_key
    upload_token = api.post(
        "/api/v1/upload-tokens",
        json={"reporter": {"name": "Marta Silva", "odoo_uid": 7}},
        headers=auth(token),
    ).json()["token"]

    body = api.post(
        "/api/v1/tickets",
        json={"title": "Impersonation attempt", "reporter": {"name": "The CEO", "odoo_uid": 1}},
        headers=auth(upload_token),
    ).json()
    assert body["reporter"]["name"] == "Marta Silva"


def test_upload_token_is_single_use(api, client_and_key):
    _, token = client_and_key
    upload_token = api.post(
        "/api/v1/upload-tokens",
        json={"reporter": {"name": "Marta Silva"}},
        headers=auth(token),
    ).json()["token"]

    assert api.post("/api/v1/tickets", json={"title": "one"}, headers=auth(upload_token)).status_code == 201
    second = api.post("/api/v1/tickets", json={"title": "two"}, headers=auth(upload_token))
    assert second.status_code == 401
    assert second.json()["error"] == "used_upload_token"


def test_upload_token_cannot_read_or_mint(api, client_and_key):
    _, token = client_and_key
    upload_token = api.post(
        "/api/v1/upload-tokens",
        json={"reporter": {"name": "Marta Silva"}},
        headers=auth(token),
    ).json()["token"]

    assert api.get("/api/v1/tickets", headers=auth(upload_token)).status_code == 403
    nested = api.post(
        "/api/v1/upload-tokens",
        json={"reporter": {"name": "Escalation"}},
        headers=auth(upload_token),
    )
    assert nested.status_code == 403


# ------------------------------------------------------- read / edit / reply


def test_tickets_are_isolated_between_clients(api, store, client_and_key):
    _, token = client_and_key
    ticket_id = api.post("/api/v1/tickets", json=MINIMAL, headers=auth(token)).json()["id"]

    other = store.create_client(name="Globex", slug="globex")
    _, other_token = store.issue_api_key(other.id, name="prod")

    assert api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(other_token)).status_code == 404
    assert api.get("/api/v1/tickets", headers=auth(other_token)).json()["items"] == []


def test_get_ticket_by_human_ref(api, client_and_key):
    _, token = client_and_key
    api.post("/api/v1/tickets", json=MINIMAL, headers=auth(token))
    assert api.get("/api/v1/tickets/ACME-0001", headers=auth(token)).json()["ref"] == "ACME-0001"


def test_list_pagination_and_search(api, client_and_key):
    _, token = client_and_key
    for index in range(5):
        api.post(
            "/api/v1/tickets",
            json={**MINIMAL, "title": f"Issue number {index}"},
            headers=auth(token),
        )

    page = api.get("/api/v1/tickets?limit=2", headers=auth(token)).json()
    assert len(page["items"]) == 2 and page["has_more"]

    seen = {item["ref"] for item in page["items"]}
    while page["next_cursor"]:
        page = api.get(
            f"/api/v1/tickets?limit=2&cursor={page['next_cursor']}", headers=auth(token)
        ).json()
        seen |= {item["ref"] for item in page["items"]}
    assert len(seen) == 5

    found = api.get("/api/v1/tickets?search=number 3", headers=auth(token)).json()
    assert [i["title"] for i in found["items"]] == ["Issue number 3"]


def test_client_may_close_and_reopen_but_not_resolve(api, client_and_key):
    _, token = client_and_key
    ticket_id = api.post("/api/v1/tickets", json=MINIMAL, headers=auth(token)).json()["id"]

    closed = api.patch(
        f"/api/v1/tickets/{ticket_id}", json={"status": "closed"}, headers=auth(token)
    ).json()
    assert closed["status"] == "closed" and closed["closed_at"]

    reopened = api.patch(
        f"/api/v1/tickets/{ticket_id}", json={"status": "open"}, headers=auth(token)
    ).json()
    assert reopened["status"] == "open" and reopened["closed_at"] is None

    rejected = api.patch(
        f"/api/v1/tickets/{ticket_id}", json={"status": "resolved"}, headers=auth(token)
    )
    assert rejected.status_code == 422  # not an allowed literal


def test_client_cannot_set_assignee(api, store, client_and_key):
    _, token = client_and_key
    ticket_id = api.post("/api/v1/tickets", json=MINIMAL, headers=auth(token)).json()["id"]
    response = api.patch(
        f"/api/v1/tickets/{ticket_id}", json={"assignee": "someone"}, headers=auth(token)
    )
    assert response.status_code == 422
    assert store.get_ticket(ticket_id).assignee is None


def test_internal_notes_are_never_exposed_to_clients(api, store, client_and_key):
    _, token = client_and_key
    ticket_id = api.post("/api/v1/tickets", json=MINIMAL, headers=auth(token)).json()["id"]

    store.add_comment(
        ticket_id,
        body="Client is on the cheap support plan, deprioritise.",
        author_type="agent",
        author_name="José",
        visibility="internal",
    )
    store.add_comment(
        ticket_id, body="Looking into it.", author_type="agent", author_name="José"
    )

    detail = api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(token)).json()
    bodies = [c["body"] for c in detail["comments"]]
    assert bodies == ["Looking into it."]

    listed = api.get(f"/api/v1/tickets/{ticket_id}/comments", headers=auth(token)).json()
    assert all("deprioritise" not in c["body"] for c in listed["items"])


def test_client_comment_round_trip(api, client_and_key):
    _, token = client_and_key
    ticket_id = api.post("/api/v1/tickets", json=MINIMAL, headers=auth(token)).json()["id"]
    response = api.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={
            "body": "Still happening after a restart.",
            "author": {"name": "Marta Silva"},
            "attachments": [
                {
                    "filename": "log.txt",
                    "content_type": "text/plain",
                    "data": base64.b64encode(b"traceback...").decode(),
                }
            ],
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    comment = response.json()
    assert comment["author_type"] == "client"
    assert comment["attachments"][0]["filename"] == "log.txt"


def test_updated_since_polling_surfaces_operator_replies(api, store, client_and_key):
    _, token = client_and_key
    ticket_id = api.post("/api/v1/tickets", json=MINIMAL, headers=auth(token)).json()["id"]
    checkpoint = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]

    assert api.get(f"/api/v1/tickets?updated_since={checkpoint}", headers=auth(token)).json()["items"] == []

    store.add_comment(ticket_id, body="On it.", author_type="agent", author_name="José")
    changed = api.get(f"/api/v1/tickets?updated_since={checkpoint}", headers=auth(token)).json()
    assert [i["id"] for i in changed["items"]] == [ticket_id]


# ------------------------------------------------------------- attachments


def test_attachment_download_is_client_scoped(api, store, client_and_key):
    _, token = client_and_key
    created = api.post(
        "/api/v1/tickets",
        json={
            **MINIMAL,
            "screenshot": {
                "filename": "err.png",
                "content_type": "image/png",
                "data": base64.b64encode(_png()).decode(),
            },
        },
        headers=auth(token),
    ).json()
    url = created["attachments"][0]["download_url"]

    ours = api.get(url, headers=auth(token))
    assert ours.status_code == 200
    assert ours.content == _png()
    assert ours.headers["content-disposition"].startswith("attachment;")
    assert ours.headers["x-content-type-options"] == "nosniff"

    other = store.create_client(name="Globex", slug="globex")
    _, other_token = store.issue_api_key(other.id, name="prod")
    assert api.get(url, headers=auth(other_token)).status_code == 404


def test_identical_screenshots_are_stored_once(api, store, client_and_key):
    _, token = client_and_key
    payload = {
        **MINIMAL,
        "screenshot": {
            "filename": "err.png",
            "content_type": "image/png",
            "data": base64.b64encode(_png()).decode(),
        },
    }
    api.post("/api/v1/tickets", json=payload, headers=auth(token))
    api.post("/api/v1/tickets", json=payload, headers=auth(token))
    blobs = list(store.settings.blob_dir.rglob("*"))
    assert len([b for b in blobs if b.is_file()]) == 1


# ------------------------------------------------------------- rate limiting


def test_rate_limit_kicks_in(settings, store):
    limited = Settings(**{**settings.__dict__, "rate_limit_per_minute": 3})
    limited_store = Store(limited)
    record = limited_store.create_client(name="Acme", slug="acme")
    _, token = limited_store.issue_api_key(record.id, name="prod")

    with TestClient(create_app(limited, limited_store)) as test_client:
        codes = [
            test_client.get("/api/v1/whoami", headers=auth(token)).status_code for _ in range(5)
        ]
    assert codes.count(200) == 3
    assert codes[-1] == 429
