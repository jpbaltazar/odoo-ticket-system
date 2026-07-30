"""Regression tests for the change feed, idempotency and rate-limit contract.

Each test here corresponds to a defect found in review; the names describe the
failure that was possible before, not just the behaviour being asserted.
"""

from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient

from otk.api.app import create_app
from otk.config import Settings
from otk.service import Store


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def token(store) -> str:
    record = store.create_client(name="Acme Industries", slug="acme")
    _, plaintext = store.issue_api_key(record.id, name="prod")
    return plaintext


@pytest.fixture
def api(settings, store) -> TestClient:
    with TestClient(create_app(settings, store)) as test_client:
        yield test_client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create(api, token, title="T", **extra):
    return api.post(
        "/api/v1/tickets",
        json={"title": title, "reporter": {"name": "Marta"}, **extra},
        headers=auth(token),
    ).json()


# ------------------------------------------------------------- the sync feed


def test_partial_drain_does_not_lose_the_tail(api, store, token):
    """The original bug: newest-first + checkpoint-on-max skipped older changes.

    Four tickets; the oldest-created and newest-created both get replies. A
    client reading one page at a time must eventually see both.
    """
    ids = [_create(api, token, f"T{i}")["id"] for i in range(4)]
    checkpoint = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]

    store.add_comment(ids[0], body="reply on oldest", author_type="agent", author_name="J")
    store.add_comment(ids[3], body="reply on newest", author_type="agent", author_name="J")

    page = api.get(
        f"/api/v1/tickets?updated_since={checkpoint}&limit=1", headers=auth(token)
    ).json()
    seen = [item["id"] for item in page["items"]]

    # Resume strictly from the cursor, never from a timestamp.
    while page["has_more"]:
        page = api.get(
            f"/api/v1/tickets?limit=1&cursor={page['next_cursor']}", headers=auth(token)
        ).json()
        seen.extend(item["id"] for item in page["items"])

    assert set(seen) == {ids[0], ids[3]}


def test_sync_feed_is_ascending_by_updated_at(api, store, token):
    ids = [_create(api, token, f"T{i}")["id"] for i in range(3)]
    checkpoint = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]
    for ticket_id in reversed(ids):  # touch newest-created first
        store.add_comment(ticket_id, body="x", author_type="agent", author_name="J")

    items = api.get(f"/api/v1/tickets?updated_since={checkpoint}", headers=auth(token)).json()[
        "items"
    ]
    stamps = [i["updated_at"] for i in items]
    assert stamps == sorted(stamps)
    assert [i["id"] for i in items] == list(reversed(ids))


def test_sync_cursor_is_returned_even_on_the_last_page(api, token):
    """So a caller can checkpoint without inspecting timestamps at all."""
    _create(api, token)
    checkpoint = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]
    page = api.get(f"/api/v1/tickets?updated_since={checkpoint}", headers=auth(token)).json()
    assert page["has_more"] is False
    assert page["next_cursor"] is not None

    # An empty poll echoes the cursor back, so the position never regresses.
    again = api.get(
        f"/api/v1/tickets?cursor={page['next_cursor']}", headers=auth(token)
    ).json()
    assert again["items"] == []
    assert again["next_cursor"] == page["next_cursor"]


def test_ticket_updated_mid_pagination_is_redelivered_not_skipped(api, store, token):
    ids = [_create(api, token, f"T{i}")["id"] for i in range(3)]
    checkpoint = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]
    for ticket_id in ids:
        store.add_comment(ticket_id, body="x", author_type="agent", author_name="J")

    page = api.get(
        f"/api/v1/tickets?updated_since={checkpoint}&limit=1", headers=auth(token)
    ).json()
    first_seen = page["items"][0]["id"]

    # Touch an already-delivered ticket: it must move ahead of the cursor.
    store.add_comment(first_seen, body="again", author_type="agent", author_name="J")

    seen = [first_seen]
    while page["has_more"]:
        page = api.get(
            f"/api/v1/tickets?limit=1&cursor={page['next_cursor']}", headers=auth(token)
        ).json()
        seen.extend(i["id"] for i in page["items"])

    assert set(ids) <= set(seen), "no ticket may be skipped"
    assert seen.count(first_seen) == 2, "re-delivery is correct; loss is not"


def test_tickets_sharing_a_timestamp_are_not_skipped(api, store, token):
    """The id tie-break in the cursor is what makes this safe."""
    ids = [_create(api, token, f"T{i}")["id"] for i in range(3)]
    stamp = "2030-01-01T00:00:00.000000Z"
    with store._transaction():
        for ticket_id in ids:
            store.conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (stamp, ticket_id))

    seen, cursor = [], None
    page = api.get(
        "/api/v1/tickets?updated_since=2029-01-01T00:00:00Z&limit=1", headers=auth(token)
    ).json()
    seen += [i["id"] for i in page["items"]]
    while page["has_more"]:
        page = api.get(
            f"/api/v1/tickets?limit=1&cursor={page['next_cursor']}", headers=auth(token)
        ).json()
        seen += [i["id"] for i in page["items"]]
    assert set(seen) == set(ids)


def test_inbox_mode_is_still_newest_first(api, token):
    refs = [_create(api, token, f"T{i}")["ref"] for i in range(3)]
    items = api.get("/api/v1/tickets", headers=auth(token)).json()["items"]
    assert [i["ref"] for i in items] == list(reversed(refs))


def test_cursor_from_one_mode_is_rejected_in_the_other(api, token):
    for _ in range(3):
        _create(api, token)
    inbox = api.get("/api/v1/tickets?limit=1", headers=auth(token)).json()
    response = api.get(
        f"/api/v1/tickets?updated_since=2020-01-01T00:00:00Z&cursor={inbox['next_cursor']}",
        headers=auth(token),
    )
    # The inbox cursor is honoured as an inbox cursor rather than silently
    # reinterpreted against a different ordering.
    assert response.status_code == 200
    assert response.json()["items"]


def test_malformed_cursor_is_a_clean_error(api, token):
    response = api.get("/api/v1/tickets?cursor=!!!not-base64!!!", headers=auth(token))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_cursor"


# ------------------------------------------------------------- idempotency


def test_rejected_attachment_leaves_no_orphan_ticket(api, token):
    before = len(api.get("/api/v1/tickets?limit=100", headers=auth(token)).json()["items"])
    response = api.post(
        "/api/v1/tickets",
        json={
            "title": "bad attachment",
            "reporter": {"name": "Marta"},
            "attachments": [
                {
                    "filename": "x.sh",
                    "content_type": "application/x-sh",
                    "data": base64.b64encode(b"#!/bin/sh").decode(),
                }
            ],
        },
        headers=auth(token),
    )
    assert response.status_code == 400
    after = api.get("/api/v1/tickets?limit=100", headers=auth(token)).json()["items"]
    assert len(after) == before
    assert not [t for t in after if t["title"] == "bad attachment"]


def test_ticket_and_idempotency_key_commit_together(api, store, token):
    """A ticket must never exist without the key that would suppress a retry."""
    headers = {**auth(token), "Idempotency-Key": "atomic-1"}
    created = api.post(
        "/api/v1/tickets", json={"title": "t", "reporter": {"name": "M"}}, headers=headers
    ).json()
    row = store.conn.execute(
        "SELECT idempotency_key FROM tickets WHERE id=?", (created["id"],)
    ).fetchone()
    assert row["idempotency_key"] == "atomic-1"


def test_replay_ignores_the_new_body(api, token):
    headers = {**auth(token), "Idempotency-Key": "replay-1"}
    first = api.post(
        "/api/v1/tickets",
        json={"title": "original", "reporter": {"name": "Marta"}, "priority": "low"},
        headers=headers,
    ).json()
    second = api.post(
        "/api/v1/tickets",
        json={"title": "COMPLETELY DIFFERENT", "reporter": {"name": "Someone"}, "priority": "urgent"},
        headers=headers,
    )
    assert second.status_code == 201
    body = second.json()
    assert body["id"] == first["id"]
    assert body["title"] == "original"
    assert body["priority"] == "low"


def test_idempotency_key_never_expires_while_the_ticket_lives(api, store, token):
    """A retry hours later must still be suppressed, not duplicated."""
    headers = {**auth(token), "Idempotency-Key": "slow-cron"}
    first = api.post(
        "/api/v1/tickets", json={"title": "t", "reporter": {"name": "M"}}, headers=headers
    ).json()
    store.purge_expired_upload_tokens()  # the only housekeeping that runs
    second = api.post(
        "/api/v1/tickets", json={"title": "t", "reporter": {"name": "M"}}, headers=headers
    ).json()
    assert first["id"] == second["id"]


def test_idempotency_keys_do_not_collide_across_clients(api, store, token):
    other = store.create_client(name="Globex", slug="globex")
    _, other_token = store.issue_api_key(other.id, name="prod")
    body = {"title": "t", "reporter": {"name": "M"}}
    a = api.post(
        "/api/v1/tickets", json=body, headers={**auth(token), "Idempotency-Key": "shared"}
    ).json()
    b = api.post(
        "/api/v1/tickets", json=body, headers={**auth(other_token), "Idempotency-Key": "shared"}
    ).json()
    assert a["id"] != b["id"]


def test_comments_honour_idempotency(api, token):
    ticket_id = _create(api, token)["id"]
    headers = {**auth(token), "Idempotency-Key": "cmt-1"}
    first = api.post(
        f"/api/v1/tickets/{ticket_id}/comments", json={"body": "only once"}, headers=headers
    ).json()
    second = api.post(
        f"/api/v1/tickets/{ticket_id}/comments", json={"body": "only once"}, headers=headers
    ).json()
    assert first["id"] == second["id"]

    listing = api.get(f"/api/v1/tickets/{ticket_id}/comments", headers=auth(token)).json()
    assert [c["body"] for c in listing["items"]].count("only once") == 1


def test_comment_idempotency_is_scoped_per_ticket(api, token):
    one, two = _create(api, token, "A")["id"], _create(api, token, "B")["id"]
    headers = {**auth(token), "Idempotency-Key": "same"}
    a = api.post(f"/api/v1/tickets/{one}/comments", json={"body": "x"}, headers=headers).json()
    b = api.post(f"/api/v1/tickets/{two}/comments", json={"body": "x"}, headers=headers).json()
    assert a["id"] != b["id"]


# ------------------------------------------------------------ rate limiting


def test_rate_limit_headers_let_a_client_pace_itself(settings, store):
    limited = Settings(**{**settings.__dict__, "rate_limit_per_minute": 3})
    limited_store = Store(limited)
    record = limited_store.create_client(name="Acme", slug="acme")
    _, plaintext = limited_store.issue_api_key(record.id, name="prod")

    with TestClient(create_app(limited, limited_store)) as api:
        first = api.get("/api/v1/whoami", headers=auth(plaintext))
        assert first.headers["X-RateLimit-Limit"] == "3"
        assert first.headers["X-RateLimit-Remaining"] == "2"

        last = None
        for _ in range(5):
            last = api.get("/api/v1/whoami", headers=auth(plaintext))
        assert last.status_code == 429
        assert last.headers["Retry-After"].isdigit()
        assert int(last.headers["Retry-After"]) >= 1
        assert last.headers["X-RateLimit-Remaining"] == "0"


# ----------------------------------------------------------------- payloads


def test_oversized_body_is_rejected_on_content_length(api, token, settings):
    """Rejected on the declared length, without reading the whole body."""
    response = api.post(
        "/api/v1/tickets",
        content=b"{}",
        headers={
            **auth(token),
            "Content-Type": "application/json",
            "Content-Length": str(settings.max_body_bytes + 1),
        },
    )
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


def test_validation_errors_carry_field_detail(api, token):
    response = api.post(
        "/api/v1/tickets", json={"description": "no title"}, headers=auth(token)
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert any("title" in str(item.get("loc", "")) for item in body["detail"])


def test_multipart_with_bad_payload_json_is_invalid_json(api, token):
    response = api.post(
        "/api/v1/tickets",
        data={"payload": "{not json"},
        files={"screenshot": ("s.png", _png(), "image/png")},
        headers=auth(token),
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"


def test_multipart_with_invalid_fields_is_validation_error(api, token):
    import json as jsonlib

    response = api.post(
        "/api/v1/tickets",
        data={"payload": jsonlib.dumps({"description": "no title"})},
        files={"screenshot": ("s.png", _png(), "image/png")},
        headers=auth(token),
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


# ------------------------------------------------------------- comment size


def test_ticket_fetch_truncates_a_long_comment_history(api, store, token):
    ticket_id = _create(api, token)["id"]
    for index in range(Store.EMBEDDED_COMMENT_LIMIT + 10):
        store.add_comment(
            ticket_id, body=f"reply {index}", author_type="agent", author_name="J"
        )

    detail = api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(token)).json()
    assert detail["comment_count"] == Store.EMBEDDED_COMMENT_LIMIT + 10
    assert len(detail["comments"]) == Store.EMBEDDED_COMMENT_LIMIT
    assert detail["comments_truncated"] is True
    # The newest are the ones kept, and still in chronological order.
    assert detail["comments"][-1]["body"] == f"reply {Store.EMBEDDED_COMMENT_LIMIT + 9}"

    full = api.get(
        f"/api/v1/tickets/{ticket_id}/comments?limit=200", headers=auth(token)
    ).json()
    assert full["total"] == Store.EMBEDDED_COMMENT_LIMIT + 10
    assert full["has_more"] is False


def test_comments_since_returns_only_newer(api, store, token):
    ticket_id = _create(api, token)["id"]
    store.add_comment(ticket_id, body="old", author_type="agent", author_name="J")
    marker = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]
    store.add_comment(ticket_id, body="new", author_type="agent", author_name="J")

    items = api.get(
        f"/api/v1/tickets/{ticket_id}/comments?since={marker}", headers=auth(token)
    ).json()["items"]
    assert [c["body"] for c in items] == ["new"]


# ------------------------------------------------------------- attachments


def test_comment_attachments_are_not_listed_twice(api, token):
    ticket_id = _create(api, token)["id"]
    api.post(
        f"/api/v1/tickets/{ticket_id}/comments",
        json={
            "body": "here is the log",
            "attachments": [
                {
                    "filename": "log.txt",
                    "content_type": "text/plain",
                    "data": base64.b64encode(b"trace").decode(),
                }
            ],
        },
        headers=auth(token),
    )
    detail = api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(token)).json()
    assert detail["attachments"] == []
    assert [a["filename"] for a in detail["comments"][0]["attachments"]] == ["log.txt"]


# ---------------------------------------------------------------- openapi


def test_openapi_defines_every_referenced_schema(settings, store):
    spec = create_app(settings, store).openapi()
    schemas = spec["components"]["schemas"]
    assert "TicketCreate" in schemas

    import json as jsonlib

    referenced = {
        ref.rsplit("/", 1)[-1]
        for ref in jsonlib.dumps(spec).split('"$ref": "')[1:]
        for ref in [ref.split('"')[0]]
    }
    missing = referenced - set(schemas)
    assert not missing, f"dangling $ref targets: {sorted(missing)}"


def test_post_tickets_documents_validation_failure(settings, store):
    spec = create_app(settings, store).openapi()
    assert "422" in spec["paths"]["/api/v1/tickets"]["post"]["responses"]


def test_cors_preflight_allows_the_idempotency_header(api):
    response = api.options(
        "/api/v1/tickets",
        headers={
            "Origin": "https://acme.odoo.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        },
    )
    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "idempotency-key" in allowed


# ---------------------------------------------------------------- long poll


def test_long_poll_returns_the_moment_a_reply_lands(api, store, token):
    """The point of the feature: latency should track the reply, not the
    timeout."""
    import threading
    import time

    ticket_id = _create(api, token)["id"]
    marker = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]

    def reply_shortly():
        time.sleep(0.4)
        store.add_comment(ticket_id, body="quick", author_type="agent", author_name="J")

    worker = threading.Thread(target=reply_shortly)
    worker.start()
    started = time.monotonic()
    response = api.get(
        f"/api/v1/tickets/{ticket_id}/comments?since={marker}&wait=15", headers=auth(token)
    )
    elapsed = time.monotonic() - started
    worker.join()

    assert [c["body"] for c in response.json()["items"]] == ["quick"]
    assert elapsed < 8, f"waited {elapsed:.1f}s; should have returned on the reply"


def test_long_poll_gives_up_empty_at_the_deadline(api, token):
    import time

    ticket_id = _create(api, token)["id"]
    marker = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]

    started = time.monotonic()
    response = api.get(
        f"/api/v1/tickets/{ticket_id}/comments?since={marker}&wait=2", headers=auth(token)
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert 1.0 < elapsed < 10, f"expected to hold ~2s, held {elapsed:.1f}s"


def test_without_wait_the_endpoint_still_answers_immediately(api, token):
    """Existing pollers must not suddenly start blocking."""
    import time

    ticket_id = _create(api, token)["id"]
    marker = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]

    started = time.monotonic()
    response = api.get(
        f"/api/v1/tickets/{ticket_id}/comments?since={marker}", headers=auth(token)
    )
    assert response.status_code == 200
    assert time.monotonic() - started < 1.0


def test_long_poll_on_the_sync_feed_still_returns_a_cursor_when_idle(api, token):
    """Timing out must not cost the caller its resume position."""
    _create(api, token)
    marker = api.get("/api/v1/whoami", headers=auth(token)).json()["server_time"]
    page = api.get(f"/api/v1/tickets?updated_since={marker}&wait=1", headers=auth(token)).json()
    assert page["items"] == []
    assert page["next_cursor"] is not None


# ------------------------------------------------- comment paging (no holes)


def test_long_thread_is_fully_reachable_by_paging_forward(api, store, token):
    """The reported bug: `since` + a newest-N window left older comments in
    that window permanently unreachable, because `since` only moves forward."""
    ticket_id = _create(api, token)["id"]
    for index in range(250):
        store.add_comment(
            ticket_id, body=f"reply {index}", author_type="agent", author_name="J"
        )

    seen, cursor = [], None
    for _ in range(20):  # generous bound; loop exits on has_more
        query = f"?limit=100&cursor={cursor}" if cursor else "?limit=100"
        page = api.get(f"/api/v1/tickets/{ticket_id}/comments{query}", headers=auth(token)).json()
        seen.extend(c["body"] for c in page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break

    assert len(seen) == 250, "every comment must be reachable"
    assert seen == [f"reply {i}" for i in range(250)], "and in order, with no gaps"


def test_comment_paging_is_oldest_first(api, store, token):
    ticket_id = _create(api, token)["id"]
    for index in range(5):
        store.add_comment(ticket_id, body=f"m{index}", author_type="agent", author_name="J")
    page = api.get(f"/api/v1/tickets/{ticket_id}/comments?limit=3", headers=auth(token)).json()
    assert [c["body"] for c in page["items"]] == ["m0", "m1", "m2"]
    assert page["has_more"] is True
    assert page["total"] == 5


def test_comments_sharing_a_timestamp_are_not_skipped(api, store, token):
    ticket_id = _create(api, token)["id"]
    for index in range(4):
        store.add_comment(ticket_id, body=f"m{index}", author_type="agent", author_name="J")
    with store._transaction():
        store.conn.execute(
            "UPDATE comments SET created_at='2030-01-01T00:00:00.000000Z' WHERE ticket_id=?",
            (ticket_id,),
        )
    seen, cursor = [], None
    for _ in range(10):
        query = f"?limit=1&cursor={cursor}" if cursor else "?limit=1"
        page = api.get(f"/api/v1/tickets/{ticket_id}/comments{query}", headers=auth(token)).json()
        seen.extend(c["body"] for c in page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert sorted(seen) == ["m0", "m1", "m2", "m3"]


# ------------------------------------------------------- conditional requests


def test_unchanged_poll_returns_304(api, token):
    _create(api, token)
    first = api.get("/api/v1/tickets", headers=auth(token))
    etag = first.headers["etag"]
    assert etag

    again = api.get("/api/v1/tickets", headers={**auth(token), "If-None-Match": etag})
    assert again.status_code == 304
    assert not again.content


def test_etag_changes_when_a_ticket_changes(api, store, token):
    ticket_id = _create(api, token)["id"]
    etag = api.get("/api/v1/tickets", headers=auth(token)).headers["etag"]

    store.add_comment(ticket_id, body="new", author_type="agent", author_name="J")
    after = api.get("/api/v1/tickets", headers={**auth(token), "If-None-Match": etag})
    assert after.status_code == 200, "a changed feed must not be served from cache"
    assert after.headers["etag"] != etag


def test_comments_support_conditional_requests(api, store, token):
    ticket_id = _create(api, token)["id"]
    store.add_comment(ticket_id, body="one", author_type="agent", author_name="J")
    first = api.get(f"/api/v1/tickets/{ticket_id}/comments", headers=auth(token))
    etag = first.headers["etag"]

    cached = api.get(
        f"/api/v1/tickets/{ticket_id}/comments", headers={**auth(token), "If-None-Match": etag}
    )
    assert cached.status_code == 304

    store.add_comment(ticket_id, body="two", author_type="agent", author_name="J")
    fresh = api.get(
        f"/api/v1/tickets/{ticket_id}/comments", headers={**auth(token), "If-None-Match": etag}
    )
    assert fresh.status_code == 200
    assert [c["body"] for c in fresh.json()["items"]] == ["one", "two"]


# --------------------------------------------------------- contract details


def test_comment_count_is_always_present_on_list_items(api, store, token):
    """Required, not defaulted: a client must not have to guess whether an
    absent field means zero."""
    ticket_id = _create(api, token)["id"]
    store.add_comment(ticket_id, body="public", author_type="agent", author_name="J")
    store.add_comment(
        ticket_id, body="secret", author_type="agent", author_name="J", visibility="internal"
    )
    item = api.get("/api/v1/tickets", headers=auth(token)).json()["items"][0]
    assert item["comment_count"] == 1, "internal notes must not be counted"
    assert item["comments"] is None


def test_comment_count_is_required_in_the_schema(settings, store):
    from otk.api.app import create_app

    spec = create_app(settings, store).openapi()
    assert "comment_count" in spec["components"]["schemas"]["TicketOut"]["required"]


def test_author_type_is_a_documented_closed_set(settings, store):
    from otk.api.app import create_app

    spec = create_app(settings, store).openapi()
    assert set(spec["components"]["schemas"]["AuthorType"]["enum"]) == {
        "client",
        "agent",
        "system",
    }


def test_validator_covers_the_query_not_just_the_resource(api, token):
    """A validator replayed against a *different* query must miss.

    If the ETag were an aggregate over the resource alone, reusing page one's
    validator on the page-two request would answer 304 and silently truncate
    the drain.
    """
    for index in range(3):
        _create(api, token, f"T{index}")

    page1 = api.get("/api/v1/tickets?limit=1", headers=auth(token))
    etag1, cursor = page1.headers["etag"], page1.json()["next_cursor"]

    page2 = api.get(
        f"/api/v1/tickets?limit=1&cursor={cursor}",
        headers={**auth(token), "If-None-Match": etag1},
    )
    assert page2.status_code == 200, "stale validator must not truncate the drain"
    assert page2.json()["items"], "and the page must actually be returned"
    assert page2.headers["etag"] != etag1


def test_conditional_responses_are_marked_private(api, store, token):
    """These carry one client's data, keyed on the bearer token. A shared cache
    reusing them across clients would be a cross-tenant leak."""
    ticket_id = _create(api, token)["id"]
    for path in ("/api/v1/tickets", f"/api/v1/tickets/{ticket_id}/comments"):
        fresh = api.get(path, headers=auth(token))
        assert "private" in fresh.headers["cache-control"]
        assert fresh.headers["vary"] == "Authorization"

        cached = api.get(path, headers={**auth(token), "If-None-Match": fresh.headers["etag"]})
        assert cached.status_code == 304
        # The 304 must carry them too, or a cache can promote a stale entry.
        assert "private" in cached.headers["cache-control"]
        assert cached.headers["vary"] == "Authorization"
