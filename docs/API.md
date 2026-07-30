# Odoo Tickets — API definition

Version `0.1.0`. This document is the contract for the Odoo-side module. Every
example below is copied from a real response of the running server, not
hand-written.

Machine-readable equivalents live next to this file:

| File | Use |
| --- | --- |
| [`openapi.yaml`](openapi.yaml) | Import into Postman/Insomnia/codegen |
| [`openapi.json`](openapi.json) | Same document, JSON |

With the server running, `GET /docs` serves a live Swagger UI and `GET /redoc`
a reference rendering.

---

## 1. Concepts

A **client** is one of my customers (one Odoo deployment). Each client gets its
own **API key**; tickets, attachments and comments are strictly partitioned by
client. There is no cross-client read path at all — a client asking for another
client's ticket gets `404`, not `403`, so the API does not confirm that the
ticket exists.

A **ticket** carries a title, a description, the **reporter** (which Odoo user
hit the problem), a **context** object describing exactly where they were, and
attachments — normally a screenshot.

---

## 2. Base URL and versioning

```
https://tickets.example.com/api/v1
```

All paths below are relative to that. The `/api/v1` prefix is frozen: additive
changes (new optional fields, new enum members) ship without a version bump, so
**the module must ignore unknown JSON fields** rather than fail on them.

`GET /health` sits outside `/api/v1` and needs no authentication.

---

## 3. Authentication

Every `/api/v1` request carries:

```http
Authorization: Bearer <token>
```

Two token types exist, distinguished by prefix.

### 3.1 `otk_…` — client API key

Long-lived, one per client, issued by me out of band. It can create, read and
edit that client's tickets.

**This key must never reach a browser.** Anyone holding it can read every
ticket that client has ever filed, including other employees' screenshots.
Store it in `ir.config_parameter` and read it only from server-side Python:

```python
key = self.env["ir.config_parameter"].sudo().get_param("odoo_tickets.api_key")
```

Do not expose it through a `/web/dataset/call_kw` reachable method, do not put
it in an asset bundle, and do not log it.

### 3.2 `ott_…` — single-use upload token

Minted by the client's Odoo backend via `POST /upload-tokens`, then handed to
the browser. It is:

- **single-use** — valid for exactly one `POST /tickets`, then dead;
- **short-lived** — 10 minutes by default;
- **write-only** — it cannot list or read tickets (`403`);
- **identity-pinned** — the reporter is fixed at mint time. If the browser
  sends a different `reporter` in the body it is silently ignored, so an
  employee cannot file a ticket as a colleague.

### 3.3 Which one to use

| | Mode A — server relay | Mode B — browser direct |
| --- | --- | --- |
| Who calls the API | Odoo backend (Python) | Browser (JS `fetch`) |
| Screenshot bytes pass through Odoo | yes | no |
| Credential used | `otk_…` | `ott_…` |
| Round trips | 1 | 2 (mint, then upload) |
| Best for | small screenshots, simplest module | large screenshots, avoiding load on the Odoo worker |

Mode A is the one to build first; it is a single HTTP call. Add Mode B if
screenshots turn out to be big enough that relaying them ties up Odoo workers.

### 3.4 Checking a key

`GET /whoami` is the "Test connection" endpoint. Wire it to a button in the
module's settings page.

```http
GET /api/v1/whoami
Authorization: Bearer otk_live_lg5kytz3gm_pt3psg…
```

```json
{
  "client_id": "cli_831f5c1cdab04d51be84f69f",
  "client_slug": "acme",
  "client_name": "Acme Industries",
  "api_key_id": "lg5kytz3gm",
  "api_key_name": "prod",
  "auth_type": "api_key",
  "server_time": "2026-07-27T10:43:53.780102Z"
}
```

`server_time` is also the correct value to store as the first `updated_since`
checkpoint (see §6.2).

---

## 4. Creating a ticket

```
POST /api/v1/tickets   →  201 Created
```

Accepts **either** `application/json` (attachments inline as base64) **or**
`multipart/form-data` (attachments as file parts). Same response either way.

### 4.1 Idempotency

Send an `Idempotency-Key` header — any stable string, e.g. the id of the Odoo
record queuing the send:

```http
Idempotency-Key: odoo-acme-8831
```

A retry with the same key returns `201` again with the **original** ticket —
same `id`, same `ref` — instead of creating a duplicate. Without it, a network
timeout on a request that actually succeeded server-side produces two tickets
on retry.

The guarantees, precisely:

| Question | Answer |
| --- | --- |
| **Atomic with the ticket?** | Yes. The key, the ticket row, and its attachment rows are written in **one transaction**. The ticket cannot exist without the key that suppresses its retry. |
| **Retention?** | For the **lifetime of the ticket**. Nothing expires or reaps keys. A retry eight hours or eight days later is still deduplicated. (The only exception is a ticket I hard-delete, which is rare and manual.) |
| **Same key, different body?** | The **stored ticket is returned unchanged**. The new body is ignored — not merged, not compared, no `409`. Fixing a bad payload requires a new key. |
| **Status on replay?** | `201`, same as a fresh create. Don't use the status to tell them apart; compare `ref`. |
| **Scope?** | Per **client** for tickets — two clients may use the same key string without colliding. |
| **Where honoured?** | `POST /tickets` and `POST /tickets/{id}/comments`. **Not** `POST /tickets/{id}/attachments` — see below. |

**Comments** honour the header too, scoped to the ticket: a retried reply
returns the original comment instead of posting twice. The same key may be
reused on a different ticket.

**`POST /attachments` does not honour it.** A retry there duplicates the
attachment row (the bytes are deduplicated by hash, so no extra storage, but
the file is listed twice). Prefer sending files with the ticket or comment they
belong to, where idempotency does apply.

Because attachments are validated *before* the transaction opens, a rejected
file leaves **no ticket behind** — a `400` means nothing was created, so it is
safe to fix and resend under a new key.

### 4.2 JSON body

```json
{
  "title": "Cannot confirm sale order SO12043",
  "description": "Confirming raises 'Insufficient stock' though 42 units are on hand.",
  "reporter": {
    "name": "Marta Silva",
    "email": "marta@acme.example",
    "login": "marta",
    "odoo_uid": 7
  },
  "priority": "high",
  "category": "bug",
  "tags": ["sales", "stock"],
  "external_ref": "HELPDESK-991",
  "context": {
    "url": "https://acme.odoo.example/odoo/sales/12043",
    "page_title": "SO12043 - Sales Order",
    "odoo_version": "17.0",
    "database": "acme-prod",
    "company_id": 1,
    "company_name": "Acme Industries SA",
    "model": "sale.order",
    "res_id": 12043,
    "view_type": "form",
    "action_xml_id": "sale.action_orders",
    "user_lang": "pt_PT",
    "user_tz": "Europe/Lisbon",
    "client_timestamp": "2026-07-27T09:14:03Z",
    "error": {
      "name": "odoo.exceptions.UserError",
      "message": "Insufficient stock for [FURN-0042] Office Chair",
      "rpc_route": "/web/dataset/call_button",
      "rpc_model": "sale.order",
      "rpc_method": "action_confirm",
      "http_status": 200
    },
    "browser": {
      "user_agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0",
      "viewport_width": 1512,
      "viewport_height": 780,
      "language": "pt-PT"
    }
  },
  "screenshot": {
    "filename": "error.png",
    "content_type": "image/png",
    "data": "<base64 PNG, or a full data: URI>"
  }
}
```

#### Top-level fields

| Field | Type | Req. | Notes |
| --- | --- | --- | --- |
| `title` | string 1–200 | **yes** | One-line summary. Shows in my inbox list. |
| `description` | string ≤20000 | no | Free text, defaults `""`. |
| `reporter` | object | **yes** for `otk_`, ignored for `ott_` | See below. |
| `priority` | enum | no | `low` \| `normal` \| `high` \| `urgent`. Default `normal`. |
| `category` | enum | no | `bug` \| `error` \| `question` \| `how_to` \| `data` \| `performance` \| `feature_request` \| `access` \| `integration` \| `other`. Default `other`. |
| `tags` | string[] ≤20 | no | Free-form, ≤50 chars each. |
| `external_ref` | string ≤100 | no | Your own reference; echoed back so you can reconcile. |
| `context` | object | no | §4.3. Open-ended — extra keys are kept and displayed. |
| `screenshot` | file object | no | The primary image. Gets `role: "screenshot"`. |
| `attachments` | file object[] ≤10 | no | Everything else. `role: "attachment"`. |

`reporter`: `name` (**required**, 1–200), `email`, `login`, `odoo_uid`,
`phone` — all optional. Send `odoo_uid` whenever you have it; it is the most
stable identity key and is what deduplicates a person across renames.

File objects: `filename`, `content_type`, `data` (base64, optionally wrapped as
`data:image/png;base64,…`), optional `caption`. If `content_type` is omitted it
is inferred from the data URI, then from the filename.

**The `title` and `reporter.name` are the only genuinely required fields.** A
minimal valid body is:

```json
{"title": "Something broke", "reporter": {"name": "Marta Silva"}}
```

### 4.3 The `context` object — the part that matters

Every field is optional, and the object accepts **extra keys you invent**. This
is the difference between a ticket I can act on and one that costs three emails,
so send everything cheap to obtain.

| Field | Type | Where it comes from |
| --- | --- | --- |
| `url` | string | `window.location.href` |
| `page_title` | string | `document.title` |
| `odoo_version` | string | `odoo.info.server_version` |
| `database` | string | `odoo.info.db` |
| `company_id` / `company_name` | int / string | active company |
| `model` | string | e.g. `sale.order` |
| `res_id` | int | current record id |
| `view_type` | string | `form` \| `list` \| `kanban` \| … |
| `action_id` / `action_xml_id` | int / string | current action |
| `menu_xml_id` | string | current menu |
| `user_lang` / `user_tz` | string | `res.users` |
| `client_timestamp` | ISO-8601 | when the user hit the problem |
| `installed_modules` | string[] ≤500 | `name-version` strings; helpful, optional |
| `error` | object | §4.3.1 |
| `browser` | object | `user_agent`, `viewport_width/height`, `screen_width/height`, `device_pixel_ratio`, `language` |

#### 4.3.1 `context.error`

Populate whenever the report was triggered by a failure rather than a user
clicking "report a problem".

| Field | Notes |
| --- | --- |
| `name` | Exception class, e.g. `odoo.exceptions.UserError` |
| `message` | ≤4000 chars |
| `traceback` | JS stack, ≤60000 |
| `server_traceback` | Python traceback from the RPC error payload, ≤60000 |
| `rpc_route` | e.g. `/web/dataset/call_button` |
| `rpc_model`, `rpc_method` | e.g. `sale.order`, `action_confirm` |
| `http_status` | int |

In Odoo 17's JS, a failed RPC rejects with an error carrying
`data.name`, `data.message`, `data.debug` (the server traceback) — map `data.debug`
to `server_traceback`.

### 4.4 Multipart form

Same data, no base64 overhead. One text part plus file parts:

| Part | Content |
| --- | --- |
| `payload` | JSON string — the body from §4.2 **without** `screenshot`/`attachments` |
| `screenshot` | the image file |
| `attachments` | repeatable file part |

```bash
curl -X POST https://tickets.example.com/api/v1/tickets \
  -H "Authorization: Bearer $OTK_KEY" \
  -H "Idempotency-Key: odoo-acme-8831" \
  -F 'payload={"title":"Cannot confirm SO12043","reporter":{"name":"Marta Silva","odoo_uid":7}}' \
  -F 'screenshot=@error.png;type=image/png' \
  -F 'attachments=@server.log;type=text/plain'
```

### 4.5 Response — `201 Created`

```json
{
  "id": "tkt_d4544d59a77847608891e819",
  "ref": "ACME-0001",
  "title": "Cannot confirm sale order SO12043",
  "description": "Confirming raises 'Insufficient stock' though 42 units are on hand.",
  "status": "new",
  "priority": "high",
  "category": "bug",
  "source": "odoo_server",
  "tags": ["sales", "stock"],
  "external_ref": "HELPDESK-991",
  "reporter": {
    "name": "Marta Silva",
    "email": "marta@acme.example",
    "login": "marta",
    "odoo_uid": 7,
    "phone": null
  },
  "context": { "...": "echoed back as sent" },
  "created_at": "2026-07-27T10:43:53.786365Z",
  "updated_at": "2026-07-27T10:43:53.786365Z",
  "resolved_at": null,
  "closed_at": null,
  "attachments": [
    {
      "id": "att_e43e66ee02d3437da3ab2527",
      "role": "screenshot",
      "filename": "error.png",
      "content_type": "image/png",
      "size_bytes": 4319,
      "width": 1280,
      "height": 720,
      "created_at": "2026-07-27T10:43:53.786566Z",
      "download_url": "/api/v1/attachments/att_e43e66ee02d3437da3ab2527"
    }
  ],
  "comments": []
}
```

`ref` is the human-facing number (`<CLIENT>-NNNN`, per-client sequence). **Show
`ref` to the user** — it is what we will both quote in email. Store `id` for
API calls.

`source` is set by the server, never by the client. The full enum:

| Value | Means |
| --- | --- |
| `odoo_server` | Relayed by the client's Odoo backend (Mode A) |
| `odoo_browser` | Uploaded with an upload token (Mode B) |
| `api` | Some other API caller — scripts, integrations |
| `manual` | I typed it in myself from a phone call or corridor conversation |
| `email` | Reserved for a future email intake; not emitted yet |

Treat it as an open enum: match the two you produce, and display the rest
as-is rather than failing.

There is **no deletion signal**. Tickets are closed, not deleted; a mirror can
treat a ticket as permanent once seen. On the rare occasion I hard-delete one
(spam, or a screenshot that should never have been sent), it simply stops
appearing — there is no tombstone in the feed, so a mirror keeps its copy.
Tell me and I'll confirm out of band.

`download_url` is **relative**. Prefix it with your base URL, and note it
requires the API key — it is not a public link.

---

## 5. The browser-direct flow (Mode B)

### Step 1 — Odoo backend mints a token

```
POST /api/v1/upload-tokens     (requires otk_…)   →  201
```

```json
{ "reporter": { "name": "Marta Silva", "odoo_uid": 7 }, "ttl_seconds": 600 }
```

```json
{
  "token": "ott_live_zjyevu3gfq_lfvrl36il5iexxwuzt5mmyylxae5dpcnb57jqbvhu7va4pg6dvwq",
  "expires_at": "2026-07-27T10:53:53.793628Z",
  "max_file_bytes": 10485760,
  "max_files": 10
}
```

Expose this through your own Odoo controller, e.g. `POST /odoo_tickets/token`,
authenticated as `user`. Build the `reporter` from `request.env.user` on the
**server** — never from anything the browser sent.

### Step 2 — browser posts the ticket

The browser `fetch`es `POST /api/v1/tickets` with
`Authorization: Bearer <ott_…>`, ideally as multipart. It omits `reporter`
entirely; the server fills it from the token.

Mint the token **when the user opens the report dialog**, not on page load —
tokens expire, and one unused token per page view is waste.

CORS is enabled for this. If your deployment restricts origins, add the client's
Odoo domain to `OTK_CORS_ORIGINS`.

---

## 6. Reading tickets back

These require an API key; upload tokens get `403`.

### 6.1 List — two modes, two orderings

```
GET /api/v1/tickets?limit=50&status=open&search=stock&updated_since=…&cursor=…
```

| Param | Notes |
| --- | --- |
| `status` | repeatable |
| `updated_since` | ISO-8601 datetime. **Presence of this switches the ordering** — see below. |
| `search` | matches title, description, ref, reporter name |
| `reporter_email` | exact, case-insensitive |
| `limit` | 1–200, default 50 |
| `cursor` | opaque; from the previous page's `next_cursor` |
| `wait` | 0–30 seconds; hold the request until something changes. Default 0. |

```json
{ "items": [ { "...": "TicketOut, with \"comments\": null" } ],
  "next_cursor": null, "has_more": false }
```

The ordering depends on the job you are doing:

| | **Inbox mode** (default) | **Sync mode** (`updated_since`, or a sync cursor) |
| --- | --- | --- |
| Order | `created_at` **DESC** — newest first | `(updated_at, id)` **ASC** — oldest change first |
| For | showing a user their recent tickets | polling for changes |
| `next_cursor` | only while `has_more` | **always**, including the last page |

Newest-first is right for a human and wrong for a change feed, so sync mode
flips it. Ascending is what makes a partial read resumable.

**Pagination is keyset, not offset.** The cursor encodes the last row's
`(sort key, id)` and the query asks for rows strictly past it. There is no
`OFFSET`, so a row inserted or updated mid-pagination cannot shift the window
and make you step over a neighbour. The `id` is part of the key, so rows sharing
a timestamp still have a total order.

A cursor also records which mode produced it, and is interpreted in that mode.
Don't mix them; a malformed cursor is `400 invalid_cursor`.

List items omit comments (`"comments": null`) — fetch the ticket for those.

### 6.2 Polling for my replies — checkpoint on the cursor

```
GET /api/v1/tickets?updated_since=1970-01-01T00:00:00Z   ← first poll only
GET /api/v1/tickets?cursor=<saved next_cursor>           ← every poll after
```

**Persist `next_cursor` and pass it back. Do not do timestamp arithmetic.**

In sync mode `next_cursor` is returned on *every* response — including the last
page, and including an empty result (where it echoes what you sent). So the loop
is simply:

```python
cursor = icp.get_param("odoo_tickets.sync_cursor")
params = {"cursor": cursor} if cursor else {"updated_since": "1970-01-01T00:00:00Z"}
while True:
    page = get("/api/v1/tickets", params={**params, "limit": 100})
    for ticket in page["items"]:
        upsert(ticket)                       # keyed on ticket["id"]
    cursor = page["next_cursor"]             # safe to save at any point
    params = {"cursor": cursor}
    if not page["has_more"]:
        break
icp.set_param("odoo_tickets.sync_cursor", cursor)
```

You may persist the cursor after *every* page rather than only at the end;
stopping halfway simply resumes there. This is the property the old
newest-first ordering did not have.

Two consequences worth knowing:

- **Delivery is at-least-once.** A ticket updated while you are paginating moves
  *ahead* of your cursor and is delivered again. Upsert on `id`; never append.
- **`has_more` is not "cursor is present".** In sync mode a cursor is always
  present. Terminate your loop on `has_more == false`.

Seeding from `whoami.server_time` also works but is strictly worse: it is the
API process's clock, and with more than one process those can drift by enough to
skip a ticket created in the gap. Starting from the epoch costs one full sync
and then never has to be reasoned about again.

Once a minute is plenty. See §8 for the rate limit and the headers that let you
pace against it.

### 6.3 Single ticket

```
GET /api/v1/tickets/{id_or_ref}
```

Accepts either `tkt_…` or `ACME-0001`. Includes `comments`.

> My **internal notes are never returned** by any client-facing endpoint. If you
> see a comment, it was written for the client to read.

### 6.4 Edit

```
PATCH /api/v1/tickets/{id_or_ref}
```

Any subset of `title`, `description`, `priority`, `category`, `tags`,
`external_ref`, and `status` — where `status` may only be `"open"` or
`"closed"`. Any other status (`resolved`, `waiting_client`, …) and any
unrecognised field including `assignee` is rejected with `422
validation_error`: whether a problem is actually fixed is my call, not the
client's.

Setting `open` clears `resolved_at`/`closed_at`.

> **Writes are strict, reads are lenient — deliberately.** §2 asks you to
> ignore unknown fields in *responses*, while this body rejects them. That
> asymmetry is the intent, not an oversight: a field I add to a response must
> not break your module, but a field *you* misspell must not be silently
> dropped, because a `PATCH` that quietly does nothing is far worse to debug
> than one that fails loudly. Same reasoning applies to `POST /tickets`.

### 6.5 Comments

```
GET  /api/v1/tickets/{id}/comments?since=…&limit=100
POST /api/v1/tickets/{id}/comments   →  201
```

`GET` returns an **envelope**, oldest first:

```json
{ "items": [ { "...": "CommentOut" } ], "total": 132, "has_more": true }
```

| Param | Notes |
| --- | --- |
| `since` | ISO-8601; only comments created after it. Use this to poll a busy ticket. |
| `limit` | 1–200, default 100. Returns the **newest** N, in chronological order. |
| `wait` | 0–30 seconds. Hold the request open until a comment arrives. Default 0 = answer immediately. |

**`wait` turns polling into a live feed.** With `wait=25` the server holds the
connection and responds the instant the operator replies, or after 25 seconds
with `"items": []` if nothing happened — loop and call again. It costs one
request against the rate limit however long it holds, so it is far cheaper
than polling every few seconds, and it is backwards compatible: omit it and
the endpoint behaves exactly as before.

`wait` works on `GET /tickets` too, picking up status changes as well as
replies. Set your HTTP client's timeout above `wait`, and see
[RECEIVING-REPLIES.md](RECEIVING-REPLIES.md) for the full loop.

`POST` body:

```json
{
  "body": "Still happening after a restart.",
  "author": { "name": "Marta Silva" },
  "attachments": [ { "filename": "log.txt", "content_type": "text/plain", "data": "<base64>" } ]
}
```

Posted comments always get `author_type: "client"`. A client comment flags the
ticket unread in my inbox, so this is the right channel for follow-ups. Honours
`Idempotency-Key` (§4.1).

**Ticket fetches cap the embedded thread.** `GET /tickets/{id}` embeds at most
the **50 most recent** comments so a long-running ticket doesn't drag its whole
history through every request. Two fields tell you where you stand:

```json
{ "comment_count": 132, "comments_truncated": true, "comments": [ "…50 newest…" ] }
```

When `comments_truncated` is true, page the rest from the comments endpoint.

### 6.6 Attachments

```
POST /api/v1/tickets/{id}/attachments      body: [ <file object>, … ]
GET  /api/v1/attachments/{attachment_id}
```

Downloads return the raw bytes with `Content-Disposition: attachment` and
`X-Content-Type-Options: nosniff`, scoped to the calling client.

**A file belongs to exactly one list.** Files sent with a ticket appear in
`ticket.attachments`; files sent with a reply appear in that comment's
`attachments` and *not* in the ticket's. Render both lists without
deduplicating.

---

## 7. Errors

Every error has the same shape and a **stable `error` code** — branch on that,
never on `message`.

```json
{ "error": "reporter_required", "message": "reporter.name is required when using an API key" }
```

`422` responses add a `detail` array with per-field validation errors.

| HTTP | `error` | Meaning / action |
| --- | --- | --- |
| 400 | `reporter_required` | Add `reporter.name`. |
| 400 | `invalid_attachment` | Bad base64, disallowed type, bytes don't match declared type, or over the per-file cap. |
| 400 | `too_many_files` | >10 files. |
| 400 | `invalid_json` | Malformed body. |
| 401 | `missing_credentials` | No `Authorization` header. |
| 401 | `invalid_credentials` | Malformed token. |
| 401 | `invalid_api_key` | Unknown key or wrong secret. **Do not retry.** |
| 401 | `revoked_api_key` / `expired_api_key` | Surface to an admin; do not retry. |
| 401 | `invalid_upload_token` / `expired_upload_token` / `used_upload_token` | Mint a fresh one. |
| 403 | `client_disabled` | Account suspended. Stop sending. |
| 403 | `insufficient_scope` | Upload token used on a read/mint endpoint. |
| 404 | `not_found` | No such ticket **for this client**. |
| 400 | `invalid_cursor` | Malformed `cursor`. Drop it and re-sync from `updated_since`. |
| 413 | `payload_too_large` | Whole request body over the cap, **or** attachments over the per-ticket cap. |
| 422 | `validation_error` | Field-level failure; see `detail`. |
| 429 | `rate_limited` | Back off by the **`Retry-After` header**. |

A `422` adds a `detail` array of per-field errors, in Pydantic's shape:

```json
{ "error": "validation_error", "message": "body failed validation",
  "detail": [ { "type": "missing", "loc": ["title"], "msg": "Field required" } ] }
```

Multipart-specific: a `payload` part that isn't parseable JSON is
`400 invalid_json`; one that parses but fails field validation is
`422 validation_error`.

### Retry policy for the module

- `429` → wait **`Retry-After` seconds** (see §8), then retry with the same
  `Idempotency-Key`.
- `5xx` → retry with exponential backoff and the same `Idempotency-Key`.
- `4xx` other than `429` → do not retry; the request will never succeed as-is.
- Never let a failed ticket send raise into the user's Odoo transaction. Queue
  it (`ir.cron`, or a `queue_job`) and report failures out of band.

---

## 8. Limits

| Limit | Default | Env var |
| --- | --- | --- |
| Per file | 10 MB | `OTK_MAX_FILE_MB` |
| Per ticket, all files | 25 MB | `OTK_MAX_TICKET_MB` |
| **Whole request body** | **36 MB** | `OTK_MAX_BODY_MB` |
| Files per request | 10 | `OTK_MAX_FILES` |
| Requests per key | 60/min (token bucket) | `OTK_RATE_LIMIT` |
| Upload token TTL | 600 s | `OTK_UPLOAD_TOKEN_TTL` |
| Comments embedded in a ticket fetch | 50 newest | — |

The body cap is checked against `Content-Length` **before** the body is read,
so an oversized upload is refused immediately rather than after transferring
36 MB over a slow link. It sits above the attachment cap to leave room for
base64 inflation (~33%) plus the JSON around it — so a 25 MB attachment set
still fits, but a `context` with 500 `installed_modules` and a 60000-char
traceback alongside it is comfortably inside too.

### Rate limit headers

Every authenticated response carries the current quota, so you can slow down
*before* being refused — which matters because the limit is per credential and
a whole company shares one key: an enthusiastic poller can otherwise starve the
error-reporting path.

| Header | Meaning |
| --- | --- |
| `X-RateLimit-Limit` | Requests per minute for this credential |
| `X-RateLimit-Remaining` | Tokens left right now |
| `X-RateLimit-Reset` | Seconds until the bucket is full again |
| `Retry-After` | **On `429` only** — seconds to wait. Honour this rather than parsing `message`. |

Allowed content types: `image/png`, `image/jpeg`, `image/webp`, `image/gif`,
`application/pdf`, `text/plain`, `text/csv`, `application/json`,
`application/zip`, `application/xml`. Anything else is rejected. Images, PDFs
and zips also have their magic bytes checked against the declared type.

Rate limiting is **per credential, not per IP**, since a client's whole company
shares one Odoo egress address.

**Compress screenshots before sending.** A full-page PNG at 2× DPR can hit
several MB; `canvas.toBlob(cb, "image/webp", 0.8)` typically lands under 300 KB
and stays perfectly readable.

---

## 9. Odoo module sketch

> **Written against Odoo 17 idiom and NOT verified on Odoo 19.** Sections 1–8
> are the contract and are accurate; this section is illustrative only. The
> client-side property paths below (`odoo.info`, `session`,
> `env.services.action.currentController`, `error.data.debug`) moved between
> 17, 18 and 19 — and a module reading a path that no longer resolves does not
> crash. It posts a ticket with an empty `context`, which looks like success.
>
> **Verify what actually arrives** rather than trusting this section:
>
> ```bash
> otk inspect          # per-field ✓/✗ for the most recent ticket
> ```
>
> If the high-value fields (`odoo_version`, `database`, `model`, `res_id`,
> `view_type`) come back `✗` while `url` is present, the JS context collector
> is reading stale paths — `url` comes from `window` and survives regardless,
> which is what makes this failure so easy to miss.

### 9.1 Settings

```python
# models/res_config_settings.py
api_url = fields.Char(config_parameter="odoo_tickets.api_url")
api_key = fields.Char(config_parameter="odoo_tickets.api_key")

def action_test_connection(self):
    self.ensure_one()
    import requests
    icp = self.env["ir.config_parameter"].sudo()
    resp = requests.get(
        icp.get_param("odoo_tickets.api_url").rstrip("/") + "/api/v1/whoami",
        headers={"Authorization": "Bearer " + icp.get_param("odoo_tickets.api_key")},
        timeout=10,
    )
    resp.raise_for_status()
    raise UserError(_("Connected as %s") % resp.json()["client_name"])
```

Mark the key field so it is never rendered back: store it write-only, or blank
it in `get_values`. It only needs to be typed once.

### 9.2 Mode A — server relay

```python
# models/odoo_ticket.py
def _send(self):
    icp = self.env["ir.config_parameter"].sudo()
    user = self.env.user
    payload = {
        "title": self.title,
        "description": self.description or "",
        "reporter": {
            "name": user.name,
            "email": user.email,
            "login": user.login,
            "odoo_uid": user.id,
        },
        "priority": self.priority,
        "category": self.category,
        "context": json.loads(self.context_json or "{}"),
    }
    files = {}
    if self.screenshot:                      # binary field, base64 in Odoo
        files["screenshot"] = ("screenshot.png", base64.b64decode(self.screenshot), "image/png")

    resp = requests.post(
        icp.get_param("odoo_tickets.api_url").rstrip("/") + "/api/v1/tickets",
        headers={
            "Authorization": "Bearer " + icp.get_param("odoo_tickets.api_key"),
            "Idempotency-Key": "odoo-%s-%s" % (self.env.cr.dbname, self.id),
        },
        data={"payload": json.dumps(payload)},
        files=files or None,
        timeout=30,
    )
    resp.raise_for_status()
    self.remote_ref = resp.json()["ref"]
```

Call it from `ir.cron` over records in a `pending` state, so a slow or down
ticket server never blocks a user's click.

### 9.3 Mode B — token controller

```python
# controllers/main.py
class OdooTicketsController(http.Controller):
    @http.route("/odoo_tickets/token", type="json", auth="user", methods=["POST"])
    def mint(self):
        icp = request.env["ir.config_parameter"].sudo()
        user = request.env.user          # never trust the browser for identity
        resp = requests.post(
            icp.get_param("odoo_tickets.api_url").rstrip("/") + "/api/v1/upload-tokens",
            headers={"Authorization": "Bearer " + icp.get_param("odoo_tickets.api_key")},
            json={"reporter": {
                "name": user.name, "email": user.email,
                "login": user.login, "odoo_uid": user.id,
            }},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # Return only what the browser needs — not the API key, not the client id.
        return {"token": data["token"],
                "expires_at": data["expires_at"],
                "url": icp.get_param("odoo_tickets.api_url")}
```

### 9.4 Gathering context in JS

```js
import { session } from "@web/session";

function collectContext(env, error) {
    const ctrl = env.services.action.currentController || {};
    return {
        url: window.location.href,
        page_title: document.title,
        odoo_version: odoo.info?.server_version,
        database: odoo.info?.db,
        company_id: session.user_companies?.current_company,
        model: ctrl.props?.resModel,
        res_id: ctrl.props?.resId,
        view_type: ctrl.view?.type,
        action_id: ctrl.action?.id,
        action_xml_id: ctrl.action?.xml_id,
        user_lang: session.user_context?.lang,
        user_tz: session.user_context?.tz,
        client_timestamp: new Date().toISOString(),
        browser: {
            user_agent: navigator.userAgent,
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            device_pixel_ratio: window.devicePixelRatio,
            language: navigator.language,
        },
        error: error && {
            name: error.name,
            message: error.message,
            traceback: error.stack,
            server_traceback: error.data?.debug,   // Odoo puts the Python traceback here
            rpc_model: error.data?.model,
            rpc_method: error.data?.method,
        },
    };
}
```

Screenshots: `html2canvas` on `document.body`, then `canvas.toBlob(cb,
"image/webp", 0.8)`. Ship `html2canvas` in your asset bundle — the ticket
server serves no scripts.

**Let the user see and confirm the screenshot before it is sent.** It may
contain payroll figures or customer PII, and it is leaving their infrastructure.
A preview plus an explicit "Send" is the difference between a helpful feature
and a data-protection incident.

To catch errors automatically, register in
`registry.category("error_handlers")` — but always prompt rather than filing
silently, or one broken view will file a hundred tickets.

---

## 10. Checklist for the module

- [ ] API key only ever read server-side via `sudo()`
- [ ] `Idempotency-Key` on every create
- [ ] Sends queued, not inline in a user transaction
- [ ] Retry only `429`/`5xx`, with backoff
- [ ] Screenshot compressed to webp/jpeg before sending
- [ ] User previews and confirms the screenshot
- [ ] `ref` shown back to the user after filing
- [ ] Unknown response fields ignored, not fatal
- [ ] "Test connection" button hitting `/whoami`
- [ ] Sync loop persists `next_cursor`; never computes a checkpoint timestamp
- [ ] Sync loop terminates on `has_more == false`, not on cursor presence
- [ ] Tickets upserted by `id` — the feed is at-least-once
- [ ] `Retry-After` honoured on `429`
- [ ] Comment thread paged when `comments_truncated` is true
