# odoo-tickets

Ticket intake for an Odoo implementation service. Clients' Odoo instances post
problems — with a screenshot and the exact page context — to an HTTP API; you
triage them in a web UI or a terminal UI.

The point of the whole thing is that a ticket arrives already carrying the
answers to "who are you, which screen, which record, what did it say" — so it
can be acted on without three rounds of email.

## Parts

| | |
| --- | --- |
| **Intake API** | `otk serve` — what the Odoo module talks to. Per-client API keys. |
| **Web UI** | `otk web` — the operator inbox. Screenshots shown inline. |
| **TUI** | `otk tui` — same inbox in a terminal, for a quick pass over SSH. |
| **CLI** | client/key/operator management, retention, storage reporting. |

Everything shares one SQLite database and one blob directory under
`OTK_DATA_DIR`, so a backup is a single directory.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

otk operator add jose                 # web login (prompts for a password)
otk client add "Acme Industries"      # registers a client, prints its API key ONCE
otk serve                             # intake API   :8787
otk web                               # operator UI  :8788
```

Try it without a real Odoo:

```bash
otk seed        # demo client + tickets with generated screenshots
otk tui
```

## For the Odoo module implementer

**[docs/API.md](docs/API.md)** is the contract — read that, not this file.
[`docs/openapi.yaml`](docs/openapi.yaml) / [`.json`](docs/openapi.json) are the
machine-readable equivalents, regenerated with `otk openapi --out <path>`.

Two integration modes, and in both the API key stays server-side in the
client's Odoo (`ir.config_parameter`):

- **Server relay** — Odoo's Python backend posts the ticket.
- **Browser direct** — Odoo's backend mints a single-use, reporter-pinned
  upload token; the browser posts the screenshot straight to the API.

There is deliberately no mode where the raw API key reaches a browser: it can
read every ticket that client has ever filed.

## Storage and retention

Screenshots are essentially all the disk usage. Files are content-addressed, so
the same screenshot sent twice is stored once.

```bash
otk usage                             # what's taking up space
otk purge --older-than 90             # dry run + confirmation
otk purge --older-than 90 --yes       # for cron
otk gc                                # blobs left by rolled-back uploads
```

`purge` strips **files** from old closed tickets and keeps the ticket text,
context and conversation — reclaiming ~all the space at a fraction of the
information loss. The web UI has the same thing at `/storage`, with a dry run
that shows exactly what would go and how much it frees.

Set `OTK_RETENTION_DAYS` and run `otk purge --auto --yes` from cron for
unattended retention. It is 0 (off) by default.

## Configuration

All optional; defaults in [`src/otk/config.py`](src/otk/config.py).

| Variable | Default | |
| --- | --- | --- |
| `OTK_DATA_DIR` | `~/.local/share/odoo-tickets` | database, blobs, pepper |
| `OTK_SECRET` | generated on first run | pepper for hashing API keys |
| `OTK_HOST` / `OTK_PORT` | `127.0.0.1` / `8787` | intake API |
| `OTK_WEB_PORT` | `8788` | operator UI |
| `OTK_MAX_FILE_MB` | `10` | per attachment |
| `OTK_MAX_TICKET_MB` | `25` | all attachments on one ticket |
| `OTK_MAX_BODY_MB` | `36` | whole request body |
| `OTK_RATE_LIMIT` | `60` | requests/min per credential |
| `OTK_RETENTION_DAYS` | `0` | age for `purge --auto`; 0 = off |
| `OTK_TZ` | server's zone | IANA zone the web UI renders times in |
| `OTK_CORS_ORIGINS` | `*` | comma-separated, for browser-direct uploads |
| `OTK_OPERATOR` | `operator` | name on TUI replies |

`OTK_SECRET` is generated into `$OTK_DATA_DIR/secret.key` (mode 0600) on first
run. Rotating it invalidates every issued API key.

**Set `OTK_TZ` to your own zone, not the server's.** Timestamps are stored in
UTC and rendered in `OTK_TZ`; left unset they follow the host, so a server in
Germany shows a reader in Portugal every time an hour ahead — all year, since
CET/CEST and WET/WEST stay one hour apart. The zone abbreviation is always
printed (`14:03 WEST`) so a misconfiguration is visible rather than silent.

```bash
OTK_TZ=Europe/Lisbon otk web
```

## Deployment notes

Both servers are plain ASGI apps; run them behind a TLS-terminating reverse
proxy. They set `proxy_headers`, so forwarded scheme and IP are honoured.

The operator UI holds every client's screenshots, which may contain payroll
figures and customer PII. It sends a strict CSP, `SameSite=Lax` HttpOnly
session cookies, CSRF tokens on every mutation, and per-username login
throttling — but **put it behind HTTPS**, or the session cookie crosses the
network in clear.

## Development

```bash
.venv/bin/python -m pytest            # ~5 seconds
```

Tests have a 60s per-test timeout (`pytest-timeout`), because the sync-feed
tests drain a cursor in a `while has_more` loop and an unbounded run is much
harder to notice than a failing one.

## Layout

```
src/otk/
  config.py      settings from the environment
  db.py          SQLite schema + migrations
  security.py    machine credentials (API keys, upload tokens, sessions)
  auth.py        operator passwords and sessions
  storage.py     content-addressed blob store, thumbnails
  service.py     all business logic — the API, web and TUI share this
  schemas.py     the public wire contract
  api/           client-facing intake API
  web/           operator web UI
  tui/           operator terminal UI
```

No logic lives in a route handler or a widget; everything goes through
`service.Store`, so the three front ends cannot disagree about what a ticket is.
