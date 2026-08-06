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

## Alerts

Push to your phone when a ticket arrives that should not wait. One HTTP POST to
[ntfy](https://ntfy.sh); no other channel and no dependency.

```bash
otk client notify acme urgent    # this client only alerts on urgent
otk client notify globex high    # this one on high and above
otk client notify noisy off      # mute entirely
otk notify-test                  # check the setup reaches your phone
```

**Clients choose a ticket's priority; you choose which priorities are worth a
push.** The threshold lives on the client record and is settable only from here
— nothing in the client API can read or change it, so a client marking
everything urgent does not decide what wakes you.

Delivery happens after the response and can never fail a ticket: an unreachable
phone is not the filer's problem.

## Triage with an assistant

`otk mcp` is an MCP server exposing the tickets to an assistant, so it can read
a report, **look at the screenshot**, and leave findings as an internal note.

```json
"otk-tickets": {
  "command": "ssh",
  "args": ["root@your-server", "/opt/odoo-tickets/.venv/bin/otk", "mcp"]
}
```

Over SSH so the database never leaves the server. Install the extra there with
`pip install -e ".[mcp]"`.

For a client that connects over the network instead — claude.ai reaches your
server from Anthropic's cloud, not from your laptop — serve it over HTTP with a
bearer token:

```bash
otk mcp-key issue jose --name claude-web   # per operator, printed once
OTK_MCP_URL=https://mcp.abansec.com otk mcp --http
```

Tokens are **per operator**, so a note written through one is signed with that
person's name (`José Mendes · triage`) rather than anonymously, and revoking one
person leaves everyone else working. They are hashed with the server pepper like
every other credential, revocable with `otk mcp-key revoke`, and die
automatically when the operator is removed.

> **HTTP mode puts every client's screenshots behind one token.** It is not
> scoped to a client and it reads everything. Serve it over TLS only, keep the
> token out of anything shared, and prefer stdio-over-SSH wherever the client
> can spawn a process.

| Reads | Annotates |
| --- | --- |
| `list_tickets` `get_ticket` `get_screenshot` `find_similar` `list_clients` | `add_internal_note` `suggest_priority` `add_tags` |

**It cannot talk to a client.** There is no tool that writes a public comment,
closes a ticket or deletes anything, and every note it writes is
`visibility="internal"` — which no client-facing endpoint returns — and is
signed so you can tell machine notes from your own (`José Mendes · triage`
over an authenticated token, `triage (automated)` over stdio). A wrong
internal note costs a moment's confusion; the same text sent to a client in
your name is a different kind of problem. A test asserts the tool list, so a
client-facing tool cannot be added by accident.

`suggest_priority` requires a reason and records it, because a priority that
changed with no explanation is worse than one that never changed.

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
| `OTK_RATE_LIMIT` | `120` | requests/min per credential |
| `OTK_RETENTION_DAYS` | `0` | age for `purge --auto`; 0 = off |
| `OTK_TZ` | server's zone | IANA zone the web UI renders times in |
| `OTK_NOTIFY_URL` | — | ntfy topic for urgent-ticket alerts; empty disables |
| `OTK_NOTIFY_TOKEN` | — | bearer token for a protected ntfy topic |
| `OTK_NOTIFY_MIN_PRIORITY` | `high` | default alert threshold; per-client override |
| `OTK_NOTIFY_INCLUDE_TITLE` | `1` | `0` pushes only the ref, not the title |
| `OTK_WEB_BASE_URL` | — | makes alerts link to the ticket |
| `OTK_MCP_URL` | — | public URL of the MCP endpoint; required for `mcp --http` |
| `OTK_MCP_PORT` | `8789` | bind port for `mcp --http` |
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
