"""Alert threshold logic and delivery.

The threshold is the operator's, not the client's: a client picks a ticket's
priority, the operator picks which priorities are worth a push.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from otk import notify
from otk.api.app import create_app
from otk.service import Principal, ServiceError, Store


@pytest.fixture
def client_and_key(store):
    record = store.create_client(name="Acme Industries", slug="acme")
    _, token = store.issue_api_key(record.id, name="prod")
    return record, token


def _principal(record) -> Principal:
    return Principal(
        auth_type="api_key",
        client_id=record.id,
        client_slug=record.slug,
        client_name=record.name,
        api_key_id="k",
        api_key_name="k",
    )


# ------------------------------------------------------------- threshold


@pytest.mark.parametrize(
    "priority,threshold,expected",
    [
        ("urgent", "high", True),
        ("high", "high", True),
        ("normal", "high", False),
        ("low", "high", False),
        ("urgent", "urgent", True),
        ("high", "urgent", False),
        ("low", "low", True),
        ("urgent", "off", False),
        ("high", "off", False),
    ],
)
def test_threshold_comparison(priority, threshold, expected):
    assert notify.meets_threshold(priority, threshold) is expected


def test_unknown_priority_errs_towards_alerting():
    """Better a spurious push than a silently swallowed ticket."""
    assert notify.meets_threshold("catastrophic", "high") is True


# ------------------------------------------------- per-client, operator-set


def test_client_threshold_overrides_the_default(settings, store, client_and_key):
    record, _ = client_and_key
    quiet = replace(settings, notify_min_priority="high")
    quiet_store = Store(quiet)

    client = quiet_store.get_client(record.id)
    assert quiet_store.should_notify(client, "high") is True

    quiet_store.set_client_notify_priority(record.id, "urgent")
    client = quiet_store.get_client(record.id)
    assert quiet_store.should_notify(client, "high") is False
    assert quiet_store.should_notify(client, "urgent") is True


def test_a_client_can_be_muted_entirely(store, client_and_key):
    record, _ = client_and_key
    store.set_client_notify_priority(record.id, "off")
    client = store.get_client(record.id)
    assert store.should_notify(client, "urgent") is False


def test_unset_client_falls_back_to_the_deployment_default(settings, store, client_and_key):
    record, _ = client_and_key
    loud = Store(replace(settings, notify_min_priority="low"))
    assert loud.get_client(record.id).notify_priority is None
    assert loud.should_notify(loud.get_client(record.id), "low") is True


def test_threshold_is_validated(store, client_and_key):
    record, _ = client_and_key
    with pytest.raises(ServiceError):
        store.set_client_notify_priority(record.id, "screaming")


def test_threshold_is_invisible_to_the_client_api(settings, store, client_and_key):
    """It is the operator's dial. A client must not read it, let alone set it."""
    record, token = client_and_key
    store.set_client_notify_priority(record.id, "urgent")
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(create_app(settings, store)) as api:
        body = api.get("/api/v1/whoami", headers=headers).json()
        assert "notify" not in str(body).lower()

        created = api.post(
            "/api/v1/tickets",
            json={"title": "t", "reporter": {"name": "M"}, "priority": "urgent"},
            headers=headers,
        ).json()
        assert "notify" not in str(created).lower()

        # And no write path exists either.
        rejected = api.patch(
            f"/api/v1/tickets/{created['id']}",
            json={"notify_priority": "off"},
            headers=headers,
        )
        assert rejected.status_code == 422


# ---------------------------------------------------------------- delivery


def test_alert_is_sent_for_a_ticket_over_the_threshold(settings, store, client_and_key, monkeypatch):
    record, _ = client_and_key
    loud = Store(replace(settings, notify_url="https://ntfy.example/t", notify_min_priority="high"))

    sent = []
    monkeypatch.setattr(notify, "send", lambda s, alert: sent.append(alert) or True)

    ticket = loud.create_ticket(
        _principal(record), title="Server down", reporter={"name": "Marta"}, priority="urgent"
    )
    assert loud.notify_new_ticket(ticket) is True
    assert len(sent) == 1
    assert sent[0].ref == ticket.ref
    assert sent[0].priority == "urgent"


def test_no_alert_below_the_threshold(settings, store, client_and_key, monkeypatch):
    record, _ = client_and_key
    loud = Store(replace(settings, notify_url="https://ntfy.example/t", notify_min_priority="high"))
    sent = []
    monkeypatch.setattr(notify, "send", lambda s, alert: sent.append(alert) or True)

    ticket = loud.create_ticket(
        _principal(record), title="Typo", reporter={"name": "Marta"}, priority="normal"
    )
    assert loud.notify_new_ticket(ticket) is False
    assert sent == []


def test_a_failing_notifier_never_breaks_ticket_creation(settings, store, client_and_key, monkeypatch):
    """The ticket is already saved; an unreachable phone is not the filer's
    problem."""
    record, token = client_and_key
    loud_settings = replace(settings, notify_url="https://ntfy.example/t", notify_min_priority="low")
    loud = Store(loud_settings)

    def explode(*_args, **_kwargs):
        raise RuntimeError("ntfy is down")

    monkeypatch.setattr(notify, "send", explode)

    with TestClient(create_app(loud_settings, loud)) as api:
        response = api.post(
            "/api/v1/tickets",
            json={"title": "still works", "reporter": {"name": "M"}, "priority": "urgent"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 201
    assert loud.get_ticket(response.json()["id"]).title == "still works"


def test_nothing_is_sent_when_notifications_are_unconfigured(settings, store, client_and_key):
    record, _ = client_and_key
    ticket = store.create_ticket(
        _principal(record), title="t", reporter={"name": "M"}, priority="urgent"
    )
    assert store.settings.notify_url == ""
    assert store.notify_new_ticket(ticket) is False


# ----------------------------------------------------------------- message


def test_message_omits_the_title_when_asked(settings):
    alert = notify.Alert(
        ref="ACME-0042",
        client_name="Acme",
        priority="urgent",
        title="Payroll export shows 48.000 EUR for J. Silva",
        reporter="Marta",
    )
    heading, body = notify.build_message(alert, include_title=True)
    assert "Payroll" in body

    heading, body = notify.build_message(alert, include_title=False)
    assert "Payroll" not in body, "the title can name a customer or a figure"
    assert "ACME-0042" in heading
    assert "Acme" in body


def test_send_reports_failure_rather_than_raising(settings):
    unreachable = replace(settings, notify_url="http://127.0.0.1:1/nope")
    alert = notify.Alert(ref="X-1", client_name="C", priority="urgent", title="t", reporter="r")
    assert notify.send(unreachable, alert) is False
