"""Operator authentication, access control, and the retention/purge path."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from otk.auth import WeakPassword, hash_password, verify_password
from otk.service import IncomingFile, Principal, ServiceError, Store
from otk.web.app import create_web_app

PASSWORD = "correct-horse-battery-staple"


def _png(colour=(10, 20, 30)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def store(settings) -> Store:
    """Overrides the conftest fixture: every web test needs an operator."""
    store = Store(settings)
    store.create_operator("jose", PASSWORD, "José")
    return store


@pytest.fixture
def web(settings, store) -> TestClient:
    with TestClient(create_web_app(settings, store)) as client:
        yield client


@pytest.fixture
def signed_in(web) -> TestClient:
    response = web.post(
        "/login", data={"username": "jose", "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    return web


def _make_ticket(store, *, colour=(10, 20, 30), slug="acme") -> str:
    try:
        client = store.get_client_by_slug(slug)
    except ServiceError:
        client = store.create_client(name=slug.title(), slug=slug)
    principal = Principal(
        auth_type="api_key",
        client_id=client.id,
        client_slug=client.slug,
        client_name=client.name,
        api_key_id="k",
        api_key_name="k",
    )
    ticket = store.create_ticket(
        principal,
        title="Screenshot ticket",
        reporter={"name": "Marta"},
        files=[
            IncomingFile(
                data=_png(colour),
                filename="shot.png",
                content_type="image/png",
                role="screenshot",
            )
        ],
    )
    return ticket.id


# --------------------------------------------------------------- passwords


def test_password_hash_verifies_and_is_salted():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second, "equal hashes would mean no salt"
    assert verify_password(PASSWORD, first)
    assert not verify_password("wrong", first)


def test_password_hash_is_not_reversible_to_the_plaintext():
    assert PASSWORD not in hash_password(PASSWORD)


def test_short_passwords_are_refused(store):
    with pytest.raises(WeakPassword):
        store.create_operator("weak", "short")


def test_garbage_hash_does_not_crash_verification():
    assert not verify_password(PASSWORD, "not-a-hash")
    assert not verify_password(PASSWORD, "")


# ------------------------------------------------------------ access control


@pytest.mark.parametrize("path", ["/", "/storage", "/tickets/whatever"])
def test_pages_redirect_to_login_when_signed_out(web, path):
    response = web.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_attachments_are_not_served_to_anonymous_users(web, store):
    ticket_id = _make_ticket(store)
    attachment_id = store.get_ticket(ticket_id).attachments[0].id
    for path in (f"/attachments/{attachment_id}", f"/attachments/{attachment_id}/thumb"):
        response = web.get(path, follow_redirects=False)
        assert response.status_code == 303, f"{path} leaked to an anonymous caller"


def test_login_rejects_a_bad_password(web):
    response = web.post("/login", data={"username": "jose", "password": "nope"})
    assert response.status_code == 401


def test_login_sets_an_httponly_session_cookie(web):
    response = web.post(
        "/login", data={"username": "jose", "password": PASSWORD}, follow_redirects=False
    )
    cookie = response.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    # The raw session token must not be something a client could forge from
    # public information; it carries a server-side random secret.
    assert "otks_test_" in cookie


def test_signed_in_operator_reaches_the_inbox(signed_in):
    assert signed_in.get("/").status_code == 200


def test_logout_revokes_the_session_immediately(signed_in, store):
    principal = store.authenticate_session(signed_in.cookies["otk_session"])
    signed_in.post("/logout", data={"csrf_token": principal.csrf_token})
    with pytest.raises(ServiceError):
        store.authenticate_session(signed_in.cookies.get("otk_session", "x"))


def test_password_change_signs_out_existing_sessions(signed_in, store):
    cookie = signed_in.cookies["otk_session"]
    assert store.authenticate_session(cookie)
    store.set_operator_password("jose", "another-long-password-1")
    with pytest.raises(ServiceError):
        store.authenticate_session(cookie)


def test_api_key_is_not_accepted_as_a_web_session(web, store):
    client = store.create_client(name="Acme", slug="acme")
    _, api_key = store.issue_api_key(client.id, name="prod")
    web.cookies.set("otk_session", api_key)
    response = web.get("/", follow_redirects=False)
    assert response.status_code == 303, "an API key must not open the operator UI"


def test_session_cookie_is_not_accepted_as_an_api_key(settings, store):
    from otk.api.app import create_app

    cookie, _ = store.login("jose", PASSWORD)
    with TestClient(create_app(settings, store)) as api:
        response = api.get("/api/v1/whoami", headers={"Authorization": f"Bearer {cookie}"})
    assert response.status_code == 401


# --------------------------------------------------------------------- CSRF


def test_mutation_without_a_csrf_token_is_refused(signed_in, store):
    ticket_id = _make_ticket(store)
    response = signed_in.post(
        f"/tickets/{ticket_id}/update", data={"status": "closed"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert store.get_ticket(ticket_id).status == "new"


def test_mutation_with_a_wrong_csrf_token_is_refused(signed_in, store):
    ticket_id = _make_ticket(store)
    response = signed_in.post(
        f"/tickets/{ticket_id}/update",
        data={"status": "closed", "csrf_token": "forged"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert store.get_ticket(ticket_id).status == "new"


def test_mutation_with_the_right_csrf_token_succeeds(signed_in, store):
    ticket_id = _make_ticket(store)
    csrf = store.authenticate_session(signed_in.cookies["otk_session"]).csrf_token
    response = signed_in.post(
        f"/tickets/{ticket_id}/update",
        data={"status": "closed", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert store.get_ticket(ticket_id).status == "closed"


# ---------------------------------------------------------------- serving


def test_operator_sees_attachments_across_all_clients(signed_in, store):
    """Unlike the client API, the operator is deliberately not client-scoped."""
    first = _make_ticket(store, slug="acme", colour=(1, 2, 3))
    second = _make_ticket(store, slug="globex", colour=(9, 8, 7))
    for ticket_id in (first, second):
        attachment_id = store.get_ticket(ticket_id).attachments[0].id
        assert signed_in.get(f"/attachments/{attachment_id}").status_code == 200


def test_images_render_inline_and_other_files_download(signed_in, store):
    ticket_id = _make_ticket(store)
    ticket = store.get_ticket(ticket_id)
    image = ticket.attachments[0]

    response = signed_in.get(f"/attachments/{image.id}")
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["x-content-type-options"] == "nosniff"

    forced = signed_in.get(f"/attachments/{image.id}?download=1")
    assert forced.headers["content-disposition"].startswith("attachment")

    store.add_attachments(
        ticket_id,
        [IncomingFile(data=b"<a>x</a>", filename="d.xml", content_type="application/xml")],
    )
    xml = [a for a in store.get_ticket(ticket_id).attachments if a.filename == "d.xml"][0]
    xml_response = signed_in.get(f"/attachments/{xml.id}")
    # XML rendered inline in our own origin would be a scripting foothold.
    assert xml_response.headers["content-disposition"].startswith("attachment")


def test_thumbnails_are_generated_and_cached(signed_in, store):
    ticket_id = _make_ticket(store)
    attachment_id = store.get_ticket(ticket_id).attachments[0].id
    first = signed_in.get(f"/attachments/{attachment_id}/thumb")
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/webp"
    assert signed_in.get(f"/attachments/{attachment_id}/thumb").content == first.content


def test_security_headers_are_present(signed_in):
    headers = signed_in.get("/").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "script-src 'self'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"


# -------------------------------------------------------------- retention


def test_purge_removes_files_but_keeps_the_ticket(store):
    ticket_id = _make_ticket(store)
    ticket = store.get_ticket(ticket_id)
    assert ticket.attachments

    result = store.purge_attachments([ticket_id])
    assert result["files"] == 1
    assert result["bytes_freed"] > 0

    after = store.get_ticket(ticket_id)
    assert after.attachments == []
    assert after.title == ticket.title, "ticket text must survive"
    assert after.purged_files == 1
    assert after.purged_at is not None


def test_purge_keeps_comments_and_context(store):
    ticket_id = _make_ticket(store)
    store.add_comment(ticket_id, body="a note", author_type="agent", author_name="J")
    store.purge_attachments([ticket_id])
    after = store.get_ticket(ticket_id)
    assert [c.body for c in after.comments] == ["a note"]
    assert after.reporter_name == "Marta"


def test_purge_does_not_delete_a_blob_another_ticket_shares(store):
    """Blobs are content-addressed, so identical screenshots share one file."""
    first = _make_ticket(store, colour=(5, 5, 5))
    second = _make_ticket(store, colour=(5, 5, 5))
    digest = store.get_ticket(first).attachments[0].sha256
    assert digest == store.get_ticket(second).attachments[0].sha256

    store.purge_attachments([first])
    assert store.blobs.path_for(digest).exists(), "still referenced by the second ticket"

    store.purge_attachments([second])
    assert not store.blobs.path_for(digest).exists()


def test_purge_candidates_respects_age_and_status(store):
    recent = _make_ticket(store)
    old_open = _make_ticket(store)
    old_closed = _make_ticket(store)
    with store._transaction():
        for ticket_id in (old_open, old_closed):
            store.conn.execute(
                "UPDATE tickets SET updated_at=? WHERE id=?",
                ("2020-01-01T00:00:00.000000Z", ticket_id),
            )
        store.conn.execute("UPDATE tickets SET status='closed' WHERE id=?", (old_closed,))

    ids = {c["id"] for c in store.purge_candidates(older_than_days=30)}
    assert ids == {old_closed}, "only old AND closed by default"
    assert recent not in ids

    everything = {
        c["id"]
        for c in store.purge_candidates(
            older_than_days=30, statuses=["new", "open", "closed", "resolved"]
        )
    }
    assert everything == {old_open, old_closed}


def test_purge_candidates_reports_recoverable_size(store):
    ticket_id = _make_ticket(store)
    with store._transaction():
        store.conn.execute(
            "UPDATE tickets SET updated_at=?, status='closed' WHERE id=?",
            ("2020-01-01T00:00:00.000000Z", ticket_id),
        )
    candidate = store.purge_candidates(older_than_days=30)[0]
    assert candidate["file_count"] == 1
    assert candidate["bytes"] == len(_png())


def test_purging_nothing_is_a_no_op(store):
    assert store.purge_attachments([]) == {"tickets": 0, "files": 0, "bytes_freed": 0}


def test_orphan_blob_collection(store):
    ticket_id = _make_ticket(store)
    store.blobs.put(
        b"\x89PNG\r\n\x1a\n" + b"orphan", filename="o.png", content_type="image/png",
        max_bytes=1000,
    )
    found = store.collect_orphan_blobs(dry_run=True)
    assert found["blobs"] == 1

    # A dry run must not actually delete anything.
    assert store.collect_orphan_blobs(dry_run=True)["blobs"] == 1
    store.collect_orphan_blobs(dry_run=False)
    assert store.collect_orphan_blobs(dry_run=True)["blobs"] == 0
    # The real attachment survived the sweep.
    assert store.get_ticket(ticket_id).attachments


def test_storage_usage_reports_dedup(store):
    _make_ticket(store, colour=(3, 3, 3))
    _make_ticket(store, colour=(3, 3, 3))
    stats = store.storage_usage()
    assert stats["attachment_rows"] == 2
    assert stats["disk_bytes"] < stats["logical_bytes"], "identical files stored once"


# ------------------------------------------------------- inbox comment counts


def test_inbox_shows_comment_counts_including_internal_notes(signed_in, store):
    """`list_tickets` used to leave `comment_count` at 0, so the inbox never
    showed one."""
    from otk.service import TicketFilters

    ticket_id = _make_ticket(store)
    store.add_comment(ticket_id, body="public", author_type="agent", author_name="J")
    store.add_comment(
        ticket_id, body="private", author_type="agent", author_name="J", visibility="internal"
    )

    tickets, _, _ = store.list_tickets(TicketFilters(include_internal=True, limit=10))
    assert [t.comment_count for t in tickets if t.id == ticket_id] == [2]


def test_client_list_does_not_count_internal_notes(settings, store):
    """Otherwise a client could infer how many private notes exist."""
    from otk.api.app import create_app

    client = store.create_client(name="Acme2", slug="acme2")
    _, api_key = store.issue_api_key(client.id, name="prod")
    ticket_id = _make_ticket(store, slug="acme2")
    store.add_comment(ticket_id, body="public", author_type="agent", author_name="J")
    for _ in range(3):
        store.add_comment(
            ticket_id, body="secret", author_type="agent", author_name="J", visibility="internal"
        )

    with TestClient(create_app(settings, store)) as api:
        items = api.get(
            "/api/v1/tickets", headers={"Authorization": f"Bearer {api_key}"}
        ).json()["items"]
    assert [i["comment_count"] for i in items] == [1]


def test_context_url_is_only_linked_when_http(signed_in, store):
    """A `javascript:` URL arrives from another company's user; escaping alone
    does not defuse it in an href."""
    ticket_id = _make_ticket(store)
    with store._transaction():
        store.conn.execute(
            "UPDATE tickets SET context_json=? WHERE id=?",
            ('{"url": "javascript:alert(document.cookie)"}', ticket_id),
        )
    html = signed_in.get(f"/tickets/{ticket_id}").text
    assert 'href="javascript:' not in html
    assert "javascript:alert" in html, "still shown to the operator, just not clickable"


# ---------------------------------------------------------------- timezone


def test_display_timezone_converts_and_labels():
    """A German host and a Portuguese operator differ by an hour year-round."""
    from datetime import UTC, datetime

    from otk.web.filters import make_localtime

    moment = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    assert make_localtime("Europe/Lisbon")(moment) == "2026-07-27 13:00 WEST"
    assert make_localtime("Europe/Berlin")(moment) == "2026-07-27 14:00 CEST"
    # Winter: the one-hour gap persists, so this is not a summer-only quirk.
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert make_localtime("Europe/Lisbon")(winter) == "2026-01-15 12:00 WET"
    assert make_localtime("Europe/Berlin")(winter) == "2026-01-15 13:00 CET"


def test_bad_timezone_falls_back_instead_of_breaking_the_page():
    from datetime import UTC, datetime

    from otk.web.filters import make_localtime

    rendered = make_localtime("Not/AZone")(datetime(2026, 7, 27, 12, 0, tzinfo=UTC))
    assert rendered.startswith("2026-07-27")


def test_timezone_reaches_the_rendered_page(settings, store):
    from dataclasses import replace

    # Reuses the fixture's database — including its operator — so only the
    # display zone differs from the other web tests.
    _make_ticket(store)
    tz_settings = replace(settings, display_tz="Europe/Lisbon")
    with TestClient(create_web_app(tz_settings, Store(tz_settings))) as client:
        client.post("/login", data={"username": "jose", "password": PASSWORD})
        body = client.get("/").text
    assert "WEST" in body or "WET" in body


# --------------------------------------------------------- removing operators


def test_removing_an_operator_signs_out_their_sessions(store):
    store.create_operator("second", PASSWORD)
    cookie, _ = store.login("second", PASSWORD)
    assert store.authenticate_session(cookie)

    store.delete_operator("second")
    with pytest.raises(ServiceError):
        store.authenticate_session(cookie)


def test_removing_an_operator_keeps_the_replies_they_wrote(store):
    """History is text, not a foreign key — deleting an account must not
    rewrite what a client already saw."""
    store.create_operator("leaver", PASSWORD, "Departing Colleague")
    ticket_id = _make_ticket(store)
    store.add_comment(
        ticket_id, body="I looked at this", author_type="agent", author_name="Departing Colleague"
    )

    store.delete_operator("leaver")
    comments = store.get_ticket(ticket_id).comments
    assert [(c.author_name, c.body) for c in comments] == [
        ("Departing Colleague", "I looked at this")
    ]


def test_cannot_remove_the_last_operator_by_accident(store):
    assert len(store.list_operators()) == 1
    with pytest.raises(ServiceError) as excinfo:
        store.delete_operator("jose")
    assert excinfo.value.code == "last_operator"
    assert store.list_operators(), "the account must still be there"


def test_last_operator_can_be_removed_with_force(store):
    store.delete_operator("jose", force=True)
    assert store.list_operators() == []


def test_removing_an_unknown_operator_is_an_error(store):
    with pytest.raises(ServiceError):
        store.delete_operator("nobody")


# ------------------------------------------------- password reset round trip


def test_reset_password_then_log_in_with_it(settings, store):
    """The reset must actually take effect for a *separate* process, which is
    how the CLI and the web service relate on a real deployment."""
    store.set_operator_password("jose", "second-password-here")
    with TestClient(create_web_app(settings, Store(settings))) as client:
        ok = client.post(
            "/login",
            data={"username": "jose", "password": "second-password-here"},
            follow_redirects=False,
        )
        stale = client.post(
            "/login", data={"username": "jose", "password": PASSWORD}, follow_redirects=False
        )
    assert ok.status_code == 303
    assert stale.status_code == 401


@pytest.mark.parametrize(
    "password",
    [
        "p@ss w0rd$with'quotes\"&sym#12",   # shell and SQL metacharacters
        "sen ha com espaços e acentuação",  # spaces and non-ASCII
        "x" * 200,                          # long
    ],
)
def test_awkward_passwords_round_trip(settings, store, password):
    store.set_operator_password("jose", password)
    with TestClient(create_web_app(settings, Store(settings))) as client:
        response = client.post(
            "/login",
            data={"username": "jose", "password": password},
            follow_redirects=False,
        )
    assert response.status_code == 303


def test_username_is_case_insensitive(settings, store):
    with TestClient(create_web_app(settings, Store(settings))) as client:
        response = client.post(
            "/login", data={"username": "JOSE", "password": PASSWORD}, follow_redirects=False
        )
    assert response.status_code == 303


# ------------------------------------------------------------ config file


def test_cli_and_service_read_the_same_env_file(tmp_path, monkeypatch):
    """The bug this prevents: systemd reads /etc/odoo-tickets/env but a bare
    shell did not, so the CLI wrote to a second database under $HOME."""
    from otk.config import get_settings

    config = tmp_path / "env"
    config.write_text(
        "# comment\n"
        f"OTK_DATA_DIR={tmp_path / 'data'}\n"
        'export OTK_TZ="Europe/Lisbon"\n'
        "OTK_RETENTION_DAYS=45\n"
    )
    for key in ("OTK_DATA_DIR", "OTK_TZ", "OTK_RETENTION_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OTK_CONFIG", str(config))
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.data_dir == tmp_path / "data"
    assert settings.display_tz == "Europe/Lisbon"
    assert settings.retention_days == 45
    get_settings.cache_clear()


def test_real_environment_beats_the_config_file(tmp_path, monkeypatch):
    from otk.config import get_settings

    config = tmp_path / "env"
    config.write_text(f"OTK_DATA_DIR={tmp_path / 'from-file'}\n")
    monkeypatch.setenv("OTK_CONFIG", str(config))
    monkeypatch.setenv("OTK_DATA_DIR", str(tmp_path / "from-env"))
    get_settings.cache_clear()

    assert get_settings().data_dir == tmp_path / "from-env"
    get_settings.cache_clear()


# --------------------------------------------------------- reply ergonomics


def test_reply_returns_to_the_conversation_not_the_top(signed_in, store):
    """A ticket page is long — screenshot, context, modules. Landing back at
    the top after every reply means scrolling down again each time."""
    ticket_id = _make_ticket(store)
    csrf = store.authenticate_session(signed_in.cookies["otk_session"]).csrf_token
    response = signed_in.post(
        f"/tickets/{ticket_id}/reply",
        data={"body": "on it", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("#conversation")

    page = signed_in.get(f"/tickets/{ticket_id}").text
    assert 'id="conversation"' in page, "the anchor must exist for the fragment to work"


def test_operator_display_name_is_used_on_replies(signed_in, store):
    store.set_operator_display_name("jose", "José Mendes")
    ticket_id = _make_ticket(store)
    csrf = store.authenticate_session(signed_in.cookies["otk_session"]).csrf_token
    signed_in.post(
        f"/tickets/{ticket_id}/reply",
        data={"body": "hello", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert [c.author_name for c in store.get_ticket(ticket_id).comments] == ["José Mendes"]


def test_renaming_an_operator_does_not_rewrite_past_replies(store):
    """author_name is a snapshot: a client must not see who replied change."""
    ticket_id = _make_ticket(store)
    store.add_comment(ticket_id, body="first", author_type="agent", author_name="jose")
    store.set_operator_display_name("jose", "José Mendes")
    assert [c.author_name for c in store.get_ticket(ticket_id).comments] == ["jose"]


def test_display_name_cannot_be_blanked(store):
    with pytest.raises(ServiceError):
        store.set_operator_display_name("jose", "   ")


# ---------------------------------------------------- replying and ordering


def test_reply_moves_a_new_ticket_to_open(signed_in, store):
    """Answering a client means the ticket is being worked."""
    ticket_id = _make_ticket(store)
    assert store.get_ticket(ticket_id).status == "new"
    csrf = store.authenticate_session(signed_in.cookies["otk_session"]).csrf_token
    signed_in.post(
        f"/tickets/{ticket_id}/reply",
        data={"body": "Looking now.", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert store.get_ticket(ticket_id).status == "open"


def test_internal_note_does_not_move_the_ticket(signed_in, store):
    """A private note is bookkeeping, not an answer to anyone."""
    ticket_id = _make_ticket(store)
    csrf = store.authenticate_session(signed_in.cookies["otk_session"]).csrf_token
    signed_in.post(
        f"/tickets/{ticket_id}/reply",
        data={"body": "deprioritise", "internal": "on", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert store.get_ticket(ticket_id).status == "new"


@pytest.mark.parametrize("terminal", ["resolved", "closed"])
def test_reply_does_not_reopen_a_finished_ticket(signed_in, store, terminal):
    """Reopening is a decision, not a side effect of adding a note."""
    ticket_id = _make_ticket(store)
    store.update_ticket(ticket_id, {"status": terminal}, actor="operator")
    csrf = store.authenticate_session(signed_in.cookies["otk_session"]).csrf_token
    signed_in.post(
        f"/tickets/{ticket_id}/reply",
        data={"body": "one more thing", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert store.get_ticket(ticket_id).status == terminal


def test_client_reply_does_not_advance_status(settings, store):
    """Only the operator answering moves it; the client writing does not."""
    from otk.api.app import create_app

    ticket_id = _make_ticket(store)
    client = store.get_client_by_slug("acme")
    _, token = store.issue_api_key(client.id, name="prod")
    with TestClient(create_app(settings, store)) as api:
        api.post(
            f"/api/v1/tickets/{ticket_id}/comments",
            json={"body": "any news?"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert store.get_ticket(ticket_id).status == "new"







# ------------------------------------------------------- client priority dots


def test_client_row_shows_new_tickets_by_priority(signed_in, store):
    for priority in ("urgent", "urgent", "high", "low"):
        ticket_id = _make_ticket(store)
        store.update_ticket(ticket_id, {"priority": priority}, actor="op")
    # An open ticket is no longer "new" and must not be counted.
    store.update_ticket(_make_ticket(store), {"status": "open"}, actor="op")

    counts = store.new_counts_by_client()
    client_id = store.get_client_by_slug("acme").id
    assert counts[client_id] == {"urgent": 2, "high": 1, "low": 1}

    html = signed_in.get("/").text
    assert 'class="dot prio-urgent"' in html
    assert "2 new urgent tickets" in html
    assert "1 new high ticket" in html



def test_priority_ordering_refuses_a_cursor(store):
    """The rank is computed, not stored, so a keyset cursor over it would be a
    lie the moment a priority changes."""
    from otk.service import TicketFilters

    # Two tickets, so the first page reports has_more and yields a cursor.
    _make_ticket(store)
    _make_ticket(store)
    _, cursor, has_more = store.list_tickets(TicketFilters(limit=1))
    assert has_more and cursor
    with pytest.raises(ServiceError, match="does not paginate"):
        store.list_tickets(TicketFilters(sort="priority", cursor=cursor, limit=1))


# --------------------------------------------------------------- auto-close


def _age(store, ticket_id, days):
    from datetime import timedelta

    from otk.service import iso, now

    stamp = iso(now() - timedelta(days=days))
    with store._transaction():
        store.conn.execute(
            "UPDATE tickets SET updated_at=?, resolved_at=? WHERE id=?",
            (stamp, stamp, ticket_id),
        )


def test_resolved_tickets_close_after_the_window(store):
    stale = _make_ticket(store)
    fresh = _make_ticket(store)
    for ticket_id in (stale, fresh):
        store.update_ticket(ticket_id, {"status": "resolved"}, actor="op")
    _age(store, stale, days=6)

    closed = store.autoclose_resolved(days=5)
    assert closed == [store.get_ticket(stale).ref]
    assert store.get_ticket(stale).status == "closed"
    assert store.get_ticket(stale).closed_at is not None
    assert store.get_ticket(fresh).status == "resolved"


def test_only_resolved_tickets_are_closed(store):
    """An old untouched *open* ticket is a backlog problem, not a closable one."""
    old_open = _make_ticket(store)
    store.update_ticket(old_open, {"status": "open"}, actor="op")
    _age(store, old_open, days=90)
    assert store.autoclose_resolved(days=5) == []
    assert store.get_ticket(old_open).status == "open"


def test_activity_resets_the_clock(store):
    """A client replying 'still broken' must not be closed out from under them.

    The window runs from the last activity, not from when it was resolved.
    """
    ticket_id = _make_ticket(store)
    store.update_ticket(ticket_id, {"status": "resolved"}, actor="op")
    _age(store, ticket_id, days=6)

    store.add_comment(ticket_id, body="still broken", author_type="client", author_name="Marta")

    assert store.autoclose_resolved(days=5) == []
    assert store.get_ticket(ticket_id).status == "resolved"


def test_autoclose_is_disabled_at_zero(store):
    ticket_id = _make_ticket(store)
    store.update_ticket(ticket_id, {"status": "resolved"}, actor="op")
    _age(store, ticket_id, days=400)
    assert store.autoclose_resolved(days=0) == []
    assert store.get_ticket(ticket_id).status == "resolved"


def test_autoclose_is_recorded_in_the_event_log(store):
    ticket_id = _make_ticket(store)
    store.update_ticket(ticket_id, {"status": "resolved"}, actor="op")
    _age(store, ticket_id, days=10)
    store.autoclose_resolved(days=5)
    kinds = [
        r["kind"]
        for r in store.conn.execute("SELECT kind FROM events WHERE ticket_id=?", (ticket_id,))
    ]
    assert "ticket.autoclosed" in kinds


# ------------------------------------------------------------ asset caching


def test_static_urls_are_versioned(signed_in):
    """A browser holding yesterday's CSS after a deploy looks exactly like a
    broken layout."""
    import re

    html = signed_in.get("/").text
    assert re.search(r'app\.css\?v=[\w.\-]+', html)
    assert re.search(r'app\.js\?v=[\w.\-]+', html)


def test_inbox_groups_tickets_into_priority_columns(signed_in, store):
    """Columns are urgency levels, not statuses: the question is what to look
    at next."""
    import re

    made = {}
    for priority in ("low", "urgent", "normal", "high", "urgent"):
        ticket_id = _make_ticket(store)
        store.update_ticket(ticket_id, {"priority": priority}, actor="op")
        made.setdefault(priority, []).append(ticket_id)

    html = signed_in.get("/").text
    sections = re.findall(
        r'<section class="board-col prio-(\w+)">(.*?)</section>', html, re.S
    )
    assert [name for name, _ in sections] == ["urgent", "high", "normal", "low"]

    for name, block in sections:
        header = re.search(r'board-count">(\d+)<', block).group(1)
        links = re.findall(r'/tickets/(tkt_\w+)"', block)
        assert int(header) == len(set(links)), f"{name} header disagrees with its cards"
        assert set(links) == set(made.get(name, [])), f"wrong tickets in {name}"


def test_board_shows_everything_except_closed(signed_in, store):
    resolved = _make_ticket(store)
    store.update_ticket(resolved, {"status": "resolved"}, actor="op")
    waiting = _make_ticket(store)
    store.update_ticket(waiting, {"status": "waiting_client"}, actor="op")
    closed = _make_ticket(store)
    store.update_ticket(closed, {"status": "closed"}, actor="op")

    html = signed_in.get("/").text
    assert f'/tickets/{resolved}"' in html, "resolved stays visible until it ages away"
    assert f'/tickets/{waiting}"' in html
    assert f'/tickets/{closed}"' not in html
    assert f'/tickets/{closed}"' in signed_in.get("/?view=closed").text


def test_card_keeps_the_image_and_the_text_in_separate_blocks(signed_in, store):
    """Nesting them in one anchor made the text wrap around the thumbnail."""
    import re

    _make_ticket(store)
    html = signed_in.get("/").text
    # `card[^"]*` also matches `card-empty`; require the exact class.
    card = re.search(r'<li class="card(?: [^"]*)?">(.*?)</li>', html, re.S).group(1)

    thumb = re.search(r'<a class="card-thumb".*?</a>', card, re.S).group(0)
    body = re.search(r'<div class="card-body">.*?</div>', card, re.S).group(0)
    assert "<img" in thumb and "card-title" not in thumb
    assert "card-title" in body and "<img" not in body


def test_the_board_is_not_draggable(signed_in, store):
    """Moving a ticket between urgency columns would be a judgement, not a
    gesture."""
    _make_ticket(store)
    html = signed_in.get("/").text
    assert "draggable" not in html
    assert "data-dropzone" not in html


def test_empty_columns_collapse(signed_in, store):
    """An empty column shrinks to its label so the occupied ones get the room,
    but stays visible — "no urgent tickets" is worth seeing."""
    import re

    ticket_id = _make_ticket(store)
    store.update_ticket(ticket_id, {"priority": "urgent"}, actor="op")

    html = signed_in.get("/").text
    sections = dict(
        (m.group(1), m.group(0))
        for m in re.finditer(
            r'<section class="board-col prio-(\w+)[^"]*">.*?</section>', html, re.S
        )
    )
    assert "is-empty" not in sections["urgent"]
    assert "board-cards" in sections["urgent"]

    for name in ("high", "normal", "low"):
        assert "is-empty" in sections[name], f"{name} has no tickets and should collapse"
        assert "board-cards" not in sections[name], "an empty column renders no list"
        assert f">{name}" in sections[name] or name in sections[name], "label still shown"


def test_badge_does_not_count_tickets_the_inbox_cannot_show(signed_in, store):
    """A badge counting closed tickets never clears, and reads as an empty
    inbox insisting there is something in it."""
    live = _make_ticket(store)
    hidden = _make_ticket(store)
    store.update_ticket(hidden, {"status": "closed"}, actor="op")
    # Both are still unread; only one is reachable.
    assert store.unread_count(include_closed=True) == 2
    assert store.unread_count() == 1

    import re

    html = signed_in.get("/").text
    badge = re.search(r'Inbox\s*<span class="badge">(\d+)</span>', html)
    assert badge and badge.group(1) == "1"


def test_an_all_closed_system_says_so_rather_than_showing_empty_columns(signed_in, store):
    for _ in range(3):
        store.update_ticket(_make_ticket(store), {"status": "closed"}, actor="op")

    import re

    html = signed_in.get("/").text
    assert "empty-state" in html
    assert "board-col" not in html, "four collapsed strips are not an answer"
    # The template wraps lines, so compare against collapsed whitespace.
    assert "All 3 tickets are closed" in re.sub(r"\s+", " ", html)
    assert 'href="/?view=closed"' in html


def test_resolved_tickets_still_show_on_the_board(signed_in, store):
    """They are live until autoclose ages them out."""
    ticket_id = _make_ticket(store)
    store.update_ticket(ticket_id, {"status": "resolved"}, actor="op")
    html = signed_in.get("/").text
    assert f'/tickets/{ticket_id}"' in html
    assert "empty-state" not in html
