"""Business logic, shared by the HTTP API and the TUI.

Everything that touches the database goes through `Store`. Neither the API
routes nor the TUI widgets write SQL of their own, so the two views of a ticket
can never drift apart.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Literal, Sequence

from . import auth, db, notify, security
from .config import Settings, get_settings
from .schemas import (
    CLIENT_SETTABLE_STATUSES,
    Category,
    Priority,
    Source,
    TicketStatus,
)
from .storage import BlobStore, StorageError, StoredBlob

AuthType = Literal["api_key", "upload_token"]


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    """Format as fixed-width UTC ISO-8601, so string ordering is time ordering.

    Microsecond precision is load-bearing, not decoration: clients poll with
    `updated_since`, and at second granularity any change landing in the same
    second as the caller's checkpoint would be skipped forever.
    """
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class ServiceError(Exception):
    """A failure with a stable error code, mapped to HTTP status by the API."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        *,
        detail: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail
        self.headers = headers or {}


class NotFound(ServiceError):
    def __init__(self, message: str = "not found") -> None:
        super().__init__("not_found", message, 404)


@dataclass(frozen=True)
class IncomingFile:
    """A file already read into memory, normalised from JSON or multipart."""

    data: bytes
    filename: str
    content_type: str
    role: str = "attachment"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller behind a request."""

    auth_type: AuthType
    client_id: str
    client_slug: str
    client_name: str
    api_key_id: str
    api_key_name: str
    upload_token_id: str | None = None
    bound_reporter: dict[str, Any] | None = None
    """Set for upload tokens: the reporter identity fixed when the client's
    Odoo backend minted the token. A browser cannot claim to be someone else."""


@dataclass
class TicketFilters:
    client_id: str | None = None
    statuses: Sequence[str] | None = None
    priorities: Sequence[str] | None = None
    search: str | None = None
    updated_since: datetime | None = None
    reporter_email: str | None = None
    unread_only: bool = False
    include_closed: bool = True
    include_internal: bool = False
    """Whether `comment_count` counts internal notes. False for anything
    client-facing: the number of private notes is not a client's business."""
    limit: int = 50
    cursor: str | None = None


@dataclass(frozen=True)
class RateLimitState:
    limit: int
    remaining: int
    reset_after: int
    """Whole seconds until the bucket has at least one token again."""


@dataclass
class ClientRecord:
    id: str
    slug: str
    name: str
    contact_email: str | None
    odoo_url: str | None
    notes: str
    active: bool
    created_at: datetime
    notify_priority: str | None = None
    """Operator-set alert threshold. None means the deployment default."""


@dataclass
class AttachmentRecord:
    id: str
    ticket_id: str
    comment_id: str | None
    role: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    width: int | None
    height: int | None
    created_at: datetime


@dataclass
class CommentRecord:
    id: str
    ticket_id: str
    author_type: str
    author_name: str
    body: str
    visibility: str
    created_at: datetime
    attachments: list[AttachmentRecord] = field(default_factory=list)


@dataclass
class TicketRecord:
    id: str
    ref: str
    client_id: str
    client_slug: str
    client_name: str
    reporter_name: str
    reporter_email: str | None
    reporter: dict[str, Any]
    title: str
    description: str
    status: str
    priority: str
    category: str
    source: str
    context: dict[str, Any]
    tags: list[str]
    assignee: str | None
    external_ref: str | None
    unread: bool
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None
    purged_at: datetime | None = None
    purged_files: int = 0
    attachments: list[AttachmentRecord] = field(default_factory=list)
    comments: list[CommentRecord] = field(default_factory=list)
    comments_truncated: bool = False
    comment_count: int = 0


# Two orderings, and a cursor is only meaningful within the one that produced
# it. The mode is carried inside the cursor so mixing them fails loudly rather
# than silently skipping rows.
INBOX_MODE = "inbox"  # created_at DESC — a human reading newest-first
SYNC_MODE = "sync"  # updated_at ASC — a resumable change feed
COMMENT_MODE = "cmt"  # created_at ASC — a thread read forwards


def _encode_cursor(mode: str, sort_value: str, ticket_id: str) -> str:
    raw = f"{mode}|{sort_value}|{ticket_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str, str]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        parts = base64.urlsafe_b64decode(padded).decode().split("|")
    except Exception as exc:
        raise ServiceError("invalid_cursor", "cursor is malformed") from exc
    if len(parts) != 3 or not all(parts) or parts[0] not in (INBOX_MODE, SYNC_MODE, COMMENT_MODE):
        raise ServiceError("invalid_cursor", "cursor is malformed")
    return parts[0], parts[1], parts[2]


class Store:
    """Database gateway. Safe to share across threads.

    Each thread gets its own SQLite connection, because a connection cannot be
    used concurrently and FastAPI runs sync endpoints in a worker pool.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.blobs = BlobStore(self.settings.blob_dir)
        self._local = threading.local()
        # Reentrant: `_transaction` holds this while calling helpers that also
        # take it. A plain Lock would deadlock on the first nested write.
        self._write_lock = threading.RLock()
        # Create the schema once up front on a throwaway connection.
        conn = db.open_database(self.settings.db_path)
        conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self.settings.db_path)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _transaction(self):
        """Run a block as one write transaction.

        BEGIN IMMEDIATE takes the write lock up front, so two concurrent
        creates serialise here instead of one failing at COMMIT.
        """
        with self._write_lock:
            already_open = self.conn.in_transaction
            if already_open:
                # Nested call: join the caller's transaction rather than
                # committing a partial unit of work out from under it.
                yield
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # ---------------------------------------------------------------- clients

    def create_client(
        self,
        *,
        name: str,
        slug: str,
        contact_email: str | None = None,
        odoo_url: str | None = None,
        notes: str = "",
    ) -> ClientRecord:
        slug = slug.strip().lower()
        if not slug.replace("-", "").replace("_", "").isalnum():
            raise ServiceError(
                "invalid_slug", "slug must contain only letters, digits, - and _"
            )
        client_id = new_id("cli")
        created = now()
        try:
            with self._write_lock:
                self.conn.execute(
                    "INSERT INTO clients(id, slug, name, contact_email, odoo_url, notes,"
                    " active, ticket_seq, created_at) VALUES(?,?,?,?,?,?,1,0,?)",
                    (client_id, slug, name, contact_email, odoo_url, notes, iso(created)),
                )
        except sqlite3.IntegrityError as exc:
            raise ServiceError("slug_taken", f"a client with slug {slug!r} exists", 409) from exc
        self._log_event(None, client_id, "operator", "client.created", {"slug": slug})
        return ClientRecord(
            id=client_id,
            slug=slug,
            name=name,
            contact_email=contact_email,
            odoo_url=odoo_url,
            notes=notes,
            active=True,
            created_at=created,
        )

    def _client_from_row(self, row: sqlite3.Row) -> ClientRecord:
        return ClientRecord(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            contact_email=row["contact_email"],
            odoo_url=row["odoo_url"],
            notes=row["notes"],
            active=bool(row["active"]),
            created_at=parse_iso(row["created_at"]),
            notify_priority=row["notify_priority"],
        )

    def get_client(self, client_id: str) -> ClientRecord:
        row = self.conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if row is None:
            raise NotFound(f"client {client_id} not found")
        return self._client_from_row(row)

    def get_client_by_slug(self, slug: str) -> ClientRecord:
        row = self.conn.execute(
            "SELECT * FROM clients WHERE slug=?", (slug.strip().lower(),)
        ).fetchone()
        if row is None:
            raise NotFound(f"client {slug!r} not found")
        return self._client_from_row(row)

    def list_clients(self, include_inactive: bool = True) -> list[ClientRecord]:
        sql = "SELECT * FROM clients"
        if not include_inactive:
            sql += " WHERE active=1"
        sql += " ORDER BY name COLLATE NOCASE"
        return [self._client_from_row(r) for r in self.conn.execute(sql)]

    def set_client_notify_priority(self, client_id: str, threshold: str | None) -> None:
        """Set the alert threshold for one client.

        Operator-only by construction: nothing in the client API reads or
        writes this, so a client filing everything as urgent cannot decide what
        reaches a phone.
        """
        if threshold is not None and threshold not in notify.THRESHOLD_VALUES:
            raise ServiceError(
                "invalid_threshold",
                f"threshold must be one of {', '.join(notify.THRESHOLD_VALUES)}, or unset",
            )
        with self._write_lock:
            cur = self.conn.execute(
                "UPDATE clients SET notify_priority=? WHERE id=?", (threshold, client_id)
            )
        if cur.rowcount == 0:
            raise NotFound(f"client {client_id} not found")

    def should_notify(self, client: ClientRecord, priority: str) -> bool:
        threshold = client.notify_priority or self.settings.notify_min_priority
        return notify.meets_threshold(priority, threshold)

    def build_alert(self, ticket: TicketRecord) -> notify.Alert:
        base = self.settings.web_base_url
        return notify.Alert(
            ref=ticket.ref,
            client_name=ticket.client_name,
            priority=ticket.priority,
            title=ticket.title,
            reporter=ticket.reporter_name,
            url=f"{base}/tickets/{ticket.id}" if base else "",
        )

    def notify_new_ticket(self, ticket: TicketRecord) -> bool:
        """Alert the operator if this ticket clears the client's threshold.

        Never raises: the ticket is already saved, and an unreachable phone is
        not the filer's problem.
        """
        try:
            client = self.get_client(ticket.client_id)
            if not self.should_notify(client, ticket.priority):
                return False
            return notify.send(self.settings, self.build_alert(ticket))
        except Exception:
            notify.log.exception("notification for %s failed", ticket.ref)
            return False

    def set_client_active(self, client_id: str, active: bool) -> None:
        with self._write_lock:
            self.conn.execute(
                "UPDATE clients SET active=? WHERE id=?", (1 if active else 0, client_id)
            )

    # --------------------------------------------------------------- api keys

    def issue_api_key(
        self, client_id: str, name: str, expires_at: datetime | None = None
    ) -> tuple[str, str]:
        """Create a key for a client. Returns (key_id, plaintext_token).

        The plaintext is returned exactly once; only its hash is persisted.
        """
        self.get_client(client_id)
        cred = security.generate_credential(
            security.API_KEY_PREFIX, self.settings.env, self.settings.secret
        )
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO api_keys(id, client_id, name, secret_hash, scopes, created_at,"
                " expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    cred.key_id,
                    client_id,
                    name,
                    cred.secret_hash,
                    json.dumps([]),
                    iso(now()),
                    iso(expires_at) if expires_at else None,
                ),
            )
        self._log_event(None, client_id, "operator", "api_key.issued", {"key_id": cred.key_id})
        return cred.key_id, cred.token

    def list_api_keys(self, client_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT k.*, c.slug AS client_slug, c.name AS client_name FROM api_keys k"
            " JOIN clients c ON c.id = k.client_id"
        )
        params: list[Any] = []
        if client_id:
            sql += " WHERE k.client_id=?"
            params.append(client_id)
        sql += " ORDER BY k.created_at DESC"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def revoke_api_key(self, key_id: str) -> None:
        with self._write_lock:
            cur = self.conn.execute(
                "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (iso(now()), key_id),
            )
        if cur.rowcount == 0:
            raise NotFound(f"api key {key_id} not found or already revoked")
        self._log_event(None, None, "operator", "api_key.revoked", {"key_id": key_id})

    # ------------------------------------------------------------------ auth

    def authenticate(self, token: str) -> Principal:
        parsed = security.parse_token(token)
        if parsed is None:
            raise ServiceError("invalid_credentials", "malformed token", 401)
        if parsed.prefix == security.API_KEY_PREFIX:
            return self._authenticate_api_key(parsed)
        return self._authenticate_upload_token(parsed)

    def _authenticate_api_key(self, parsed: security.ParsedToken) -> Principal:
        row = self.conn.execute(
            "SELECT k.*, c.slug AS client_slug, c.name AS client_name, c.active AS client_active"
            " FROM api_keys k JOIN clients c ON c.id=k.client_id WHERE k.id=?",
            (parsed.key_id,),
        ).fetchone()
        # Verify against a dummy hash when the id is unknown so that a bad id
        # and a bad secret take the same time and leak nothing.
        expected = row["secret_hash"] if row else "0" * 64
        valid = security.verify_secret(parsed.secret, expected, self.settings.secret)
        if row is None or not valid:
            raise ServiceError("invalid_api_key", "API key is not valid", 401)
        if row["revoked_at"]:
            raise ServiceError("revoked_api_key", "API key has been revoked", 401)
        expires = parse_iso(row["expires_at"])
        if expires and expires < now():
            raise ServiceError("expired_api_key", "API key has expired", 401)
        if not row["client_active"]:
            raise ServiceError("client_disabled", "client account is disabled", 403)

        with self._write_lock:
            self.conn.execute(
                "UPDATE api_keys SET last_used_at=? WHERE id=?", (iso(now()), parsed.key_id)
            )
        return Principal(
            auth_type="api_key",
            client_id=row["client_id"],
            client_slug=row["client_slug"],
            client_name=row["client_name"],
            api_key_id=row["id"],
            api_key_name=row["name"],
        )

    def _authenticate_upload_token(self, parsed: security.ParsedToken) -> Principal:
        row = self.conn.execute(
            "SELECT t.*, c.slug AS client_slug, c.name AS client_name, c.active AS client_active"
            " FROM upload_tokens t JOIN clients c ON c.id=t.client_id WHERE t.id=?",
            (parsed.key_id,),
        ).fetchone()
        expected = row["secret_hash"] if row else "0" * 64
        valid = security.verify_secret(parsed.secret, expected, self.settings.secret)
        if row is None or not valid:
            raise ServiceError("invalid_upload_token", "upload token is not valid", 401)
        if row["used_at"]:
            raise ServiceError("used_upload_token", "upload token has already been used", 401)
        expires = parse_iso(row["expires_at"])
        if expires and expires < now():
            raise ServiceError("expired_upload_token", "upload token has expired", 401)
        if not row["client_active"]:
            raise ServiceError("client_disabled", "client account is disabled", 403)
        return Principal(
            auth_type="upload_token",
            client_id=row["client_id"],
            client_slug=row["client_slug"],
            client_name=row["client_name"],
            api_key_id=row["api_key_id"] or "",
            api_key_name="upload-token",
            upload_token_id=row["id"],
            bound_reporter=json.loads(row["reporter_json"]),
        )

    def create_upload_token(
        self, principal: Principal, reporter: dict[str, Any], ttl_seconds: int | None = None
    ) -> tuple[str, datetime]:
        if principal.auth_type != "api_key":
            raise ServiceError(
                "insufficient_scope", "upload tokens require API key authentication", 403
            )
        ttl = ttl_seconds or self.settings.upload_token_ttl_seconds
        cred = security.generate_credential(
            security.UPLOAD_TOKEN_PREFIX, self.settings.env, self.settings.secret
        )
        expires = now() + timedelta(seconds=ttl)
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO upload_tokens(id, client_id, api_key_id, secret_hash,"
                " reporter_json, created_at, expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    cred.key_id,
                    principal.client_id,
                    principal.api_key_id,
                    cred.secret_hash,
                    json.dumps(reporter),
                    iso(now()),
                    iso(expires),
                ),
            )
        return cred.token, expires

    def purge_expired_upload_tokens(self) -> int:
        with self._write_lock:
            cur = self.conn.execute(
                "DELETE FROM upload_tokens WHERE expires_at < ?",
                (iso(now() - timedelta(days=1)),),
            )
        return cur.rowcount

    # ------------------------------------------------------------ rate limits

    def check_rate_limit(self, bucket: str, per_minute: int | None = None) -> RateLimitState:
        """Token-bucket limiter, persisted so it survives a restart.

        Returns the remaining quota on success and raises with `Retry-After`
        on refusal, so a caller can back off before being refused rather than
        having to parse the message.
        """
        capacity = per_minute or self.settings.rate_limit_per_minute
        if capacity <= 0:
            return RateLimitState(limit=0, remaining=0, reset_after=0)
        refill_per_second = capacity / 60.0
        timestamp = time.time()
        with self._write_lock:
            row = self.conn.execute(
                "SELECT tokens, updated_at FROM rate_limits WHERE bucket=?", (bucket,)
            ).fetchone()
            if row is None:
                tokens = float(capacity)
            else:
                tokens = min(
                    capacity, row["tokens"] + (timestamp - row["updated_at"]) * refill_per_second
                )
            if tokens < 1.0:
                retry_after = max(1, int((1.0 - tokens) / refill_per_second) + 1)
                raise ServiceError(
                    "rate_limited",
                    f"too many requests; retry in {retry_after}s",
                    429,
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(capacity),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(retry_after),
                    },
                )
            tokens -= 1.0
            self.conn.execute(
                "INSERT INTO rate_limits(bucket, tokens, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(bucket) DO UPDATE SET tokens=excluded.tokens,"
                " updated_at=excluded.updated_at",
                (bucket, tokens, timestamp),
            )
        # Seconds until the bucket is full again, which is what a caller pacing
        # itself against the window actually wants.
        reset_after = max(0, int((capacity - tokens) / refill_per_second) + 1)
        return RateLimitState(limit=capacity, remaining=int(tokens), reset_after=reset_after)

    # -------------------------------------------------------------- reporters

    def _upsert_reporter(self, client_id: str, reporter: dict[str, Any]) -> str | None:
        name = (reporter.get("name") or "").strip()
        if not name:
            return None
        # Prefer the most stable identifier available so the same person is not
        # recorded twice when, say, their display name changes.
        if reporter.get("odoo_uid") is not None:
            identity = f"uid:{reporter['odoo_uid']}"
        elif reporter.get("login"):
            identity = f"login:{reporter['login']}".lower()
        elif reporter.get("email"):
            identity = f"email:{reporter['email']}".lower()
        else:
            identity = f"name:{name}".lower()

        stamp = iso(now())
        with self._write_lock:
            row = self.conn.execute(
                "SELECT id FROM reporters WHERE client_id=? AND identity_key=?",
                (client_id, identity),
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE reporters SET name=?, email=COALESCE(?, email),"
                    " login=COALESCE(?, login), last_seen_at=? WHERE id=?",
                    (name, reporter.get("email"), reporter.get("login"), stamp, row["id"]),
                )
                return row["id"]
            reporter_id = new_id("rep")
            self.conn.execute(
                "INSERT INTO reporters(id, client_id, identity_key, odoo_uid, login, email,"
                " name, first_seen_at, last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    reporter_id,
                    client_id,
                    identity,
                    reporter.get("odoo_uid"),
                    reporter.get("login"),
                    reporter.get("email"),
                    name,
                    stamp,
                    stamp,
                ),
            )
            return reporter_id

    # ---------------------------------------------------------------- tickets

    def _next_ref(self, client_id: str, slug: str) -> str:
        """Allocate the next per-client ticket number, e.g. ACME-0007.

        Assumes the caller already opened a transaction, so the increment and
        the ticket insert that consumes it commit together.
        """
        self.conn.execute("UPDATE clients SET ticket_seq = ticket_seq + 1 WHERE id=?", (client_id,))
        seq = self.conn.execute(
            "SELECT ticket_seq FROM clients WHERE id=?", (client_id,)
        ).fetchone()["ticket_seq"]
        prefix = "".join(ch for ch in slug.upper() if ch.isalnum())[:8] or "TKT"
        return f"{prefix}-{seq:04d}"

    def _prepare_files(self, files: Sequence[IncomingFile]) -> list[StoredBlob]:
        """Validate and persist blobs *before* any database write.

        Split out from the row insert so a rejected attachment cannot leave a
        ticket behind. Blobs written here and then rolled back are orphaned,
        which is harmless: they are content-addressed, so a later upload of the
        same bytes reuses them.
        """
        if len(files) > self.settings.max_files_per_ticket:
            raise ServiceError(
                "too_many_files",
                f"at most {self.settings.max_files_per_ticket} files per request",
            )
        total = sum(len(f.data) for f in files)
        if total > self.settings.max_ticket_bytes:
            raise ServiceError(
                "payload_too_large",
                f"attachments total {total} bytes, over the"
                f" {self.settings.max_ticket_bytes} byte limit",
                413,
            )
        prepared = []
        for item in files:
            try:
                prepared.append(
                    self.blobs.put(
                        item.data,
                        filename=item.filename,
                        content_type=item.content_type,
                        max_bytes=self.settings.max_file_bytes,
                    )
                )
            except StorageError as exc:
                raise ServiceError("invalid_attachment", str(exc)) from exc
        return prepared

    def _insert_attachments(
        self,
        prepared: Sequence[StoredBlob],
        roles: Sequence[str],
        ticket_id: str,
        comment_id: str | None = None,
    ) -> list[AttachmentRecord]:
        stamp = now()
        stored: list[AttachmentRecord] = []
        for blob, role in zip(prepared, roles):
            record = AttachmentRecord(
                id=new_id("att"),
                ticket_id=ticket_id,
                comment_id=comment_id,
                role=role,
                filename=blob.filename,
                content_type=blob.content_type,
                size_bytes=blob.size_bytes,
                sha256=blob.sha256,
                width=blob.width,
                height=blob.height,
                created_at=stamp,
            )
            self.conn.execute(
                "INSERT INTO attachments(id, ticket_id, comment_id, role, filename,"
                " content_type, size_bytes, sha256, width, height, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    ticket_id,
                    comment_id,
                    record.role,
                    record.filename,
                    record.content_type,
                    record.size_bytes,
                    record.sha256,
                    record.width,
                    record.height,
                    iso(stamp),
                ),
            )
            stored.append(record)
        return stored

    def _store_files(
        self, files: Sequence[IncomingFile], ticket_id: str, comment_id: str | None = None
    ) -> list[AttachmentRecord]:
        prepared = self._prepare_files(files)
        with self._transaction():
            return self._insert_attachments(
                prepared, [f.role for f in files], ticket_id, comment_id
            )

    def create_ticket(
        self,
        principal: Principal,
        *,
        title: str,
        description: str = "",
        reporter: dict[str, Any] | None = None,
        priority: str = Priority.NORMAL,
        category: str = Category.OTHER,
        tags: Iterable[str] = (),
        external_ref: str | None = None,
        context: dict[str, Any] | None = None,
        files: Sequence[IncomingFile] = (),
        idempotency_key: str | None = None,
    ) -> TicketRecord:
        # An upload token pins the reporter at mint time, so a browser holding
        # one cannot file a ticket under a colleague's name.
        if principal.auth_type == "upload_token":
            reporter = dict(principal.bound_reporter or {})
            source = Source.ODOO_BROWSER
        else:
            source = Source.ODOO_SERVER
            if not reporter or not (reporter.get("name") or "").strip():
                raise ServiceError(
                    "reporter_required", "reporter.name is required when using an API key"
                )

        if idempotency_key:
            existing = self.conn.execute(
                "SELECT id FROM tickets WHERE client_id=? AND idempotency_key=?",
                (principal.client_id, idempotency_key),
            ).fetchone()
            if existing:
                return self.get_ticket(existing["id"], client_id=principal.client_id)

        client = self.get_client(principal.client_id)
        # Validate and write blobs first: an attachment the server refuses must
        # not leave a ticket behind, and this keeps the failure out of the
        # transaction entirely.
        prepared = self._prepare_files(files)
        ticket_id = new_id("tkt")
        stamp = now()
        reporter = reporter or {}

        try:
            with self._transaction():
                ref = self._next_ref(client.id, client.slug)
                reporter_id = self._upsert_reporter(client.id, reporter)
                self.conn.execute(
                    "INSERT INTO tickets(id, ref, client_id, reporter_id, reporter_name,"
                    " reporter_email, title, description, status, priority, category, source,"
                    " context_json, tags_json, external_ref, idempotency_key, unread,"
                    " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        ticket_id,
                        ref,
                        client.id,
                        reporter_id,
                        (reporter.get("name") or "Unknown").strip(),
                        reporter.get("email"),
                        title.strip(),
                        description,
                        TicketStatus.NEW,
                        str(priority),
                        str(category),
                        str(source),
                        json.dumps(context or {}, default=str),
                        json.dumps(sorted({t for t in tags if t})),
                        external_ref,
                        idempotency_key,
                        iso(stamp),
                        iso(stamp),
                    ),
                )
                # Same transaction as the ticket row: the idempotency key can
                # never be lost while the ticket survives, which is the whole
                # failure mode the header exists to prevent.
                self._insert_attachments(prepared, [f.role for f in files], ticket_id)
                if principal.upload_token_id:
                    self.conn.execute(
                        "UPDATE upload_tokens SET used_at=?, ticket_id=? WHERE id=?",
                        (iso(stamp), ticket_id, principal.upload_token_id),
                    )
        except sqlite3.IntegrityError:
            # Lost a race with a concurrent retry carrying the same key.
            if idempotency_key:
                existing = self.conn.execute(
                    "SELECT id FROM tickets WHERE client_id=? AND idempotency_key=?",
                    (principal.client_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self.get_ticket(existing["id"], client_id=principal.client_id)
            raise

        self._log_event(
            ticket_id, client.id, f"client:{principal.client_slug}", "ticket.created", {"ref": ref}
        )
        return self.get_ticket(ticket_id)

    def _attachments_for(
        self, ticket_id: str, ticket_level_only: bool = False
    ) -> list[AttachmentRecord]:
        """Files on a ticket.

        `ticket_level_only` excludes files posted with a comment, which belong
        to that comment's own list — without it a file sent alongside a reply
        renders twice in any UI that shows both.
        """
        sql = "SELECT * FROM attachments WHERE ticket_id=?"
        if ticket_level_only:
            sql += " AND comment_id IS NULL"
        rows = self.conn.execute(sql + " ORDER BY created_at, role DESC", (ticket_id,))
        return [
            AttachmentRecord(
                id=r["id"],
                ticket_id=r["ticket_id"],
                comment_id=r["comment_id"],
                role=r["role"],
                filename=r["filename"],
                content_type=r["content_type"],
                size_bytes=r["size_bytes"],
                sha256=r["sha256"],
                width=r["width"],
                height=r["height"],
                created_at=parse_iso(r["created_at"]),
            )
            for r in rows
        ]

    def _ticket_from_row(self, row: sqlite3.Row) -> TicketRecord:
        return TicketRecord(
            id=row["id"],
            ref=row["ref"],
            client_id=row["client_id"],
            client_slug=row["client_slug"],
            client_name=row["client_name"],
            reporter_name=row["reporter_name"],
            reporter_email=row["reporter_email"],
            reporter={
                "name": row["reporter_name"],
                "email": row["reporter_email"],
                "login": row["reporter_login"],
                "odoo_uid": row["reporter_uid"],
            },
            title=row["title"],
            description=row["description"],
            status=row["status"],
            priority=row["priority"],
            category=row["category"],
            source=row["source"],
            context=json.loads(row["context_json"] or "{}"),
            tags=json.loads(row["tags_json"] or "[]"),
            assignee=row["assignee"],
            external_ref=row["external_ref"],
            unread=bool(row["unread"]),
            created_at=parse_iso(row["created_at"]),
            updated_at=parse_iso(row["updated_at"]),
            resolved_at=parse_iso(row["resolved_at"]),
            closed_at=parse_iso(row["closed_at"]),
            purged_at=parse_iso(row["purged_at"]),
            purged_files=row["purged_files"],
        )

    _TICKET_SELECT = (
        "SELECT t.*, c.slug AS client_slug, c.name AS client_name,"
        " r.login AS reporter_login, r.odoo_uid AS reporter_uid"
        " FROM tickets t JOIN clients c ON c.id=t.client_id"
        " LEFT JOIN reporters r ON r.id=t.reporter_id"
    )

    # How many comments a ticket fetch embeds before truncating. Older ones
    # stay reachable through the comments endpoint.
    EMBEDDED_COMMENT_LIMIT = 50

    def get_ticket(
        self,
        ticket_id: str,
        *,
        client_id: str | None = None,
        include_internal: bool = False,
        comment_limit: int | None = EMBEDDED_COMMENT_LIMIT,
    ) -> TicketRecord:
        sql = self._TICKET_SELECT + " WHERE (t.id=? OR t.ref=?)"
        params: list[Any] = [ticket_id, ticket_id]
        if client_id:
            sql += " AND t.client_id=?"
            params.append(client_id)
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFound(f"ticket {ticket_id} not found")
        ticket = self._ticket_from_row(row)
        ticket.attachments = self._attachments_for(ticket.id, ticket_level_only=True)
        ticket.comment_count = self.count_comments(ticket.id, include_internal=include_internal)
        ticket.comments = self.list_comments(
            ticket.id, include_internal=include_internal, limit=comment_limit
        )
        ticket.comments_truncated = len(ticket.comments) < ticket.comment_count
        return ticket

    def list_tickets(
        self, filters: TicketFilters
    ) -> tuple[list[TicketRecord], str | None, bool]:
        """Return a page of tickets, the cursor to resume from, and `has_more`.

        `has_more` is reported separately rather than inferred from the cursor:
        in sync mode a cursor is always returned (so a caller can checkpoint on
        the final page), and conflating the two makes a drain loop never end.

        Two orderings, chosen by whether this is a sync request:

        * **inbox** (default) — `created_at DESC`. What a human wants.
        * **sync** (`updated_since` or a sync cursor) — `(updated_at, id) ASC`.
          Ascending is what makes a change feed correct: a partial read is
          resumable from the cursor, and a ticket updated mid-pagination moves
          *ahead* of the cursor, so it is re-delivered rather than skipped.
          Newest-first would move it behind the cursor and lose it for good.
        """
        cursor_value = cursor_id = None
        if filters.cursor:
            mode, cursor_value, cursor_id = _decode_cursor(filters.cursor)
        elif filters.updated_since is not None:
            mode = SYNC_MODE
        else:
            mode = INBOX_MODE

        sql = self._TICKET_SELECT + " WHERE 1=1"
        params: list[Any] = []
        if filters.client_id:
            sql += " AND t.client_id=?"
            params.append(filters.client_id)
        if filters.statuses:
            sql += f" AND t.status IN ({','.join('?' * len(filters.statuses))})"
            params.extend(filters.statuses)
        elif not filters.include_closed:
            sql += " AND t.status NOT IN ('closed', 'resolved')"
        if filters.priorities:
            sql += f" AND t.priority IN ({','.join('?' * len(filters.priorities))})"
            params.extend(filters.priorities)
        if filters.unread_only:
            sql += " AND t.unread=1"
        if filters.reporter_email:
            sql += " AND lower(t.reporter_email)=?"
            params.append(filters.reporter_email.lower())
        if filters.search:
            like = f"%{filters.search.strip()}%"
            sql += (
                " AND (t.title LIKE ? OR t.description LIKE ? OR t.ref LIKE ?"
                " OR t.reporter_name LIKE ?)"
            )
            params.extend([like, like, like, like])
        if mode == SYNC_MODE:
            if cursor_value is not None:
                # Keyset, not offset: the id tie-breaks tickets sharing a
                # timestamp, so a concurrent write cannot shift the window and
                # make us step over a row.
                sql += " AND (t.updated_at, t.id) > (?, ?)"
                params.extend([cursor_value, cursor_id])
            elif filters.updated_since is not None:
                sql += " AND t.updated_at > ?"
                params.append(iso(filters.updated_since))
            order = " ORDER BY t.updated_at ASC, t.id ASC"
        else:
            if cursor_value is not None:
                sql += " AND (t.created_at, t.id) < (?, ?)"
                params.extend([cursor_value, cursor_id])
            order = " ORDER BY t.created_at DESC, t.id DESC"

        limit = max(1, min(filters.limit, 200))
        sql += order + " LIMIT ?"
        params.append(limit + 1)  # one extra row tells us whether more exist

        rows = list(self.conn.execute(sql, params))
        has_more = len(rows) > limit
        rows = rows[:limit]
        tickets = []
        for row in rows:
            ticket = self._ticket_from_row(row)
            ticket.attachments = self._attachments_for(ticket.id, ticket_level_only=True)
            tickets.append(ticket)

        if tickets:
            # One aggregate for the whole page rather than a count per ticket.
            ids = [t.id for t in tickets]
            count_sql = (
                f"SELECT ticket_id, COUNT(*) AS n FROM comments"
                f" WHERE ticket_id IN ({','.join('?' * len(ids))})"
            )
            if not filters.include_internal:
                count_sql += " AND visibility='public'"
            counts = {
                r["ticket_id"]: r["n"]
                for r in self.conn.execute(count_sql + " GROUP BY ticket_id", ids)
            }
            for ticket in tickets:
                ticket.comment_count = counts.get(ticket.id, 0)

        if mode == SYNC_MODE:
            # Always hand back a resumable position, even on the last page, so
            # a caller never has to do timestamp arithmetic to checkpoint.
            if tickets:
                next_cursor = _encode_cursor(
                    SYNC_MODE, iso(tickets[-1].updated_at), tickets[-1].id
                )
            elif filters.cursor:
                next_cursor = filters.cursor
            else:
                # "-" sorts below every character used in an id, so this reads
                # as "start of the feed at this timestamp".
                next_cursor = _encode_cursor(
                    SYNC_MODE, iso(filters.updated_since or datetime.min.replace(tzinfo=UTC)), "-"
                )
        else:
            next_cursor = (
                _encode_cursor(INBOX_MODE, iso(tickets[-1].created_at), tickets[-1].id)
                if has_more and tickets
                else None
            )
        return tickets, next_cursor, has_more

    def update_ticket(
        self,
        ticket_id: str,
        changes: dict[str, Any],
        *,
        client_id: str | None = None,
        actor: str = "operator",
    ) -> TicketRecord:
        """Apply a partial update. `client_id` restricts to that client's tickets."""
        ticket = self.get_ticket(ticket_id, client_id=client_id)

        allowed = {
            "title",
            "description",
            "priority",
            "category",
            "status",
            "tags",
            "external_ref",
            "assignee",
        }
        if client_id is not None:
            # Clients get a narrower surface: no assignment, no arbitrary status.
            allowed -= {"assignee"}
            status = changes.get("status")
            if status is not None and status not in CLIENT_SETTABLE_STATUSES:
                raise ServiceError(
                    "invalid_status",
                    "clients may only set status to 'open' or 'closed'",
                    403,
                )

        sets: list[str] = []
        params: list[Any] = []
        stamp = now()
        for key, value in changes.items():
            if key not in allowed or value is None:
                continue
            if key == "tags":
                sets.append("tags_json=?")
                params.append(json.dumps(sorted({t.strip() for t in value if t.strip()})))
                continue
            sets.append(f"{key}=?")
            params.append(str(value) if hasattr(value, "value") else value)

        if not sets:
            return ticket

        new_status = changes.get("status")
        if new_status == TicketStatus.RESOLVED and not ticket.resolved_at:
            sets.append("resolved_at=?")
            params.append(iso(stamp))
        if new_status == TicketStatus.CLOSED and not ticket.closed_at:
            sets.append("closed_at=?")
            params.append(iso(stamp))
        if new_status in (TicketStatus.OPEN, TicketStatus.NEW):
            # Reopening must clear the terminal timestamps or the ticket keeps
            # reporting as resolved to anything that filters on them.
            sets.extend(["resolved_at=NULL", "closed_at=NULL"])

        sets.append("updated_at=?")
        params.append(iso(stamp))
        params.append(ticket.id)

        with self._write_lock:
            self.conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=?", params)
        self._log_event(ticket.id, ticket.client_id, actor, "ticket.updated", changes)
        return self.get_ticket(ticket.id, include_internal=client_id is None)

    def mark_read(self, ticket_id: str, read: bool = True) -> None:
        with self._write_lock:
            self.conn.execute(
                "UPDATE tickets SET unread=? WHERE id=?", (0 if read else 1, ticket_id)
            )

    def delete_ticket(self, ticket_id: str) -> None:
        """Operator-only hard delete, including orphaned blobs."""
        ticket = self.get_ticket(ticket_id)
        digests = {a.sha256 for a in ticket.attachments}
        with self._write_lock:
            self.conn.execute("DELETE FROM tickets WHERE id=?", (ticket.id,))
        for digest in digests:
            row = self.conn.execute(
                "SELECT 1 FROM attachments WHERE sha256=? LIMIT 1", (digest,)
            ).fetchone()
            self.blobs.delete_if_unreferenced(digest, still_referenced=row is not None)
        self._log_event(None, ticket.client_id, "operator", "ticket.deleted", {"ref": ticket.ref})

    # --------------------------------------------------------------- comments

    def add_comment(
        self,
        ticket_id: str,
        *,
        body: str,
        author_type: str,
        author_name: str,
        visibility: str = "public",
        files: Sequence[IncomingFile] = (),
        client_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CommentRecord:
        ticket = self.get_ticket(ticket_id, client_id=client_id)
        if client_id is not None and visibility != "public":
            raise ServiceError("invalid_visibility", "clients may only post public comments", 403)

        if idempotency_key:
            existing = self.conn.execute(
                "SELECT id FROM comments WHERE ticket_id=? AND idempotency_key=?",
                (ticket.id, idempotency_key),
            ).fetchone()
            if existing:
                return self._comment_by_id(existing["id"])

        prepared = self._prepare_files(files)
        comment_id = new_id("cmt")
        stamp = now()
        try:
            with self._transaction():
                self.conn.execute(
                    "INSERT INTO comments(id, ticket_id, author_type, author_name, body,"
                    " visibility, idempotency_key, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        comment_id,
                        ticket.id,
                        author_type,
                        author_name,
                        body,
                        visibility,
                        idempotency_key,
                        iso(stamp),
                    ),
                )
                updates = ["updated_at=?"]
                params: list[Any] = [iso(stamp)]
                if author_type == "client":
                    # A client reply makes the ticket actionable again.
                    updates.append("unread=1")
                self.conn.execute(
                    f"UPDATE tickets SET {', '.join(updates)} WHERE id=?", [*params, ticket.id]
                )
                if author_type == "agent" and visibility == "public":
                    self.conn.execute(
                        "UPDATE tickets SET first_response_at=? WHERE id=?"
                        " AND first_response_at IS NULL",
                        (iso(stamp), ticket.id),
                    )
                attachments = self._insert_attachments(
                    prepared, [f.role for f in files], ticket.id, comment_id
                )
        except sqlite3.IntegrityError:
            if idempotency_key:
                existing = self.conn.execute(
                    "SELECT id FROM comments WHERE ticket_id=? AND idempotency_key=?",
                    (ticket.id, idempotency_key),
                ).fetchone()
                if existing:
                    return self._comment_by_id(existing["id"])
            raise

        self._log_event(ticket.id, ticket.client_id, author_name, "comment.added", {})
        return CommentRecord(
            id=comment_id,
            ticket_id=ticket.id,
            author_type=author_type,
            author_name=author_name,
            body=body,
            visibility=visibility,
            created_at=stamp,
            attachments=attachments,
        )

    def _comment_row(self, row: sqlite3.Row, attachments: list[AttachmentRecord]) -> CommentRecord:
        return CommentRecord(
            id=row["id"],
            ticket_id=row["ticket_id"],
            author_type=row["author_type"],
            author_name=row["author_name"],
            body=row["body"],
            visibility=row["visibility"],
            created_at=parse_iso(row["created_at"]),
            attachments=attachments,
        )

    def _comment_by_id(self, comment_id: str) -> CommentRecord:
        row = self.conn.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone()
        if row is None:
            raise NotFound(f"comment {comment_id} not found")
        attachments = [
            a for a in self._attachments_for(row["ticket_id"]) if a.comment_id == comment_id
        ]
        return self._comment_row(row, attachments)

    def list_comments(
        self,
        ticket_id: str,
        include_internal: bool = False,
        *,
        limit: int | None = None,
        since: datetime | None = None,
    ) -> list[CommentRecord]:
        """Comments oldest-first.

        `limit` keeps the newest N: a ticket that has been going for months
        should not force its entire history through every fetch.
        """
        sql = "SELECT * FROM comments WHERE ticket_id=?"
        params: list[Any] = [ticket_id]
        if not include_internal:
            sql += " AND visibility='public'"
        if since is not None:
            sql += " AND created_at > ?"
            params.append(iso(since))

        if limit is not None:
            # Take the newest `limit`, then flip back to chronological order.
            sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
            params.append(limit)
            rows = list(self.conn.execute(sql, params))[::-1]
        else:
            rows = list(self.conn.execute(sql + " ORDER BY created_at, id", params))

        by_comment: dict[str, list[AttachmentRecord]] = {}
        for att in self._attachments_for(ticket_id):
            if att.comment_id:
                by_comment.setdefault(att.comment_id, []).append(att)
        return [self._comment_row(row, by_comment.get(row["id"], [])) for row in rows]

    def page_comments(
        self,
        ticket_id: str,
        *,
        include_internal: bool = False,
        limit: int = 100,
        since: datetime | None = None,
        cursor: str | None = None,
    ) -> tuple[list[CommentRecord], str | None, bool]:
        """Read a thread forwards, oldest first, with a resumable cursor.

        `list_comments(limit=N)` returns the newest N, which is right for the
        preview embedded in a ticket but wrong as an API window: combined with
        a `since` that only moves forward, any comment older than the newest N
        becomes permanently unreachable on a long thread. Paging forward from
        a keyset cursor has no such hole.
        """
        sql = "SELECT * FROM comments WHERE ticket_id=?"
        params: list[Any] = [ticket_id]
        if not include_internal:
            sql += " AND visibility='public'"

        if cursor:
            mode, value, comment_id = _decode_cursor(cursor)
            if mode != COMMENT_MODE:
                raise ServiceError("invalid_cursor", "not a comment cursor")
            # id tie-breaks comments sharing a timestamp, so none is stepped over.
            sql += " AND (created_at, id) > (?, ?)"
            params.extend([value, comment_id])
        elif since is not None:
            sql += " AND created_at > ?"
            params.append(iso(since))

        limit = max(1, min(limit, 200))
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(limit + 1)

        rows = list(self.conn.execute(sql, params))
        has_more = len(rows) > limit
        rows = rows[:limit]

        by_comment: dict[str, list[AttachmentRecord]] = {}
        for att in self._attachments_for(ticket_id):
            if att.comment_id:
                by_comment.setdefault(att.comment_id, []).append(att)
        items = [self._comment_row(row, by_comment.get(row["id"], [])) for row in rows]

        next_cursor = (
            _encode_cursor(COMMENT_MODE, iso(items[-1].created_at), items[-1].id)
            if items
            else cursor
        )
        return items, next_cursor, has_more

    def count_comments(self, ticket_id: str, include_internal: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM comments WHERE ticket_id=?"
        if not include_internal:
            sql += " AND visibility='public'"
        return self.conn.execute(sql, (ticket_id,)).fetchone()["n"]

    # ------------------------------------------------------------ attachments

    def add_attachments(
        self,
        ticket_id: str,
        files: Sequence[IncomingFile],
        *,
        client_id: str | None = None,
    ) -> list[AttachmentRecord]:
        ticket = self.get_ticket(ticket_id, client_id=client_id)
        stored = self._store_files(files, ticket.id)
        with self._write_lock:
            self.conn.execute(
                "UPDATE tickets SET updated_at=? WHERE id=?", (iso(now()), ticket.id)
            )
        return stored

    def get_attachment(
        self, attachment_id: str, *, client_id: str | None = None
    ) -> tuple[AttachmentRecord, bytes]:
        sql = (
            "SELECT a.* FROM attachments a JOIN tickets t ON t.id=a.ticket_id WHERE a.id=?"
        )
        params: list[Any] = [attachment_id]
        if client_id:
            sql += " AND t.client_id=?"
            params.append(client_id)
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFound(f"attachment {attachment_id} not found")
        record = AttachmentRecord(
            id=row["id"],
            ticket_id=row["ticket_id"],
            comment_id=row["comment_id"],
            role=row["role"],
            filename=row["filename"],
            content_type=row["content_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            width=row["width"],
            height=row["height"],
            created_at=parse_iso(row["created_at"]),
        )
        try:
            return record, self.blobs.read(record.sha256)
        except StorageError as exc:
            raise NotFound(str(exc)) from exc

    def attachment_path(self, attachment_id: str):
        record, _ = self.get_attachment(attachment_id)
        return self.blobs.path_for(record.sha256)

    # ------------------------------------------------------------------ stats

    def client_watermark(self, client_id: str) -> tuple[int, str]:
        """Cheapest possible "has anything changed for this client" probe.

        One indexed aggregate, used to answer a conditional request with 304
        without building the page. `updated_at` moves on every mutation and the
        count moves on create/delete, so together they cannot miss a change.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at), '') AS m"
            " FROM tickets WHERE client_id=?",
            (client_id,),
        ).fetchone()
        return row["n"], row["m"]

    def comment_watermark(self, ticket_id: str, include_internal: bool = False) -> tuple[int, str]:
        sql = (
            "SELECT COUNT(*) AS n, COALESCE(MAX(created_at), '') AS m"
            " FROM comments WHERE ticket_id=?"
        )
        if not include_internal:
            sql += " AND visibility='public'"
        row = self.conn.execute(sql, (ticket_id,)).fetchone()
        return row["n"], row["m"]

    def counts_by_status(self, client_id: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM tickets"
        params: list[Any] = []
        if client_id:
            sql += " WHERE client_id=?"
            params.append(client_id)
        sql += " GROUP BY status"
        return {row["status"]: row["n"] for row in self.conn.execute(sql, params)}

    def unread_count(self, client_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM tickets WHERE unread=1"
        params: list[Any] = []
        if client_id:
            sql += " AND client_id=?"
            params.append(client_id)
        return self.conn.execute(sql, params).fetchone()["n"]

    # -------------------------------------------------------------- operators

    def create_operator(
        self, username: str, password: str, display_name: str = ""
    ) -> str:
        auth.check_password_policy(password)
        operator_id = new_id("opr")
        try:
            with self._write_lock:
                self.conn.execute(
                    "INSERT INTO operators(id, username, password_hash, display_name,"
                    " created_at) VALUES(?,?,?,?,?)",
                    (
                        operator_id,
                        username.strip().lower(),
                        auth.hash_password(password),
                        display_name or username,
                        iso(now()),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ServiceError("username_taken", f"operator {username!r} exists", 409) from exc
        return operator_id

    def set_operator_password(self, username: str, password: str) -> None:
        auth.check_password_policy(password)
        with self._write_lock:
            cur = self.conn.execute(
                "UPDATE operators SET password_hash=? WHERE username=?",
                (auth.hash_password(password), username.strip().lower()),
            )
        if cur.rowcount == 0:
            raise NotFound(f"operator {username!r} not found")
        # Changing a password invalidates every existing session: the usual
        # reason to change it is that one may be in the wrong hands.
        self.revoke_operator_sessions(username)

    def set_operator_display_name(self, username: str, display_name: str) -> None:
        """Change the name shown on replies.

        Only affects future comments: `comments.author_name` is a snapshot, so
        a client never sees a past reply retroactively change who wrote it.
        """
        display_name = display_name.strip()
        if not display_name:
            raise ServiceError("invalid_name", "display name cannot be empty")
        with self._write_lock:
            cur = self.conn.execute(
                "UPDATE operators SET display_name=? WHERE username=?",
                (display_name, username.strip().lower()),
            )
        if cur.rowcount == 0:
            raise NotFound(f"operator {username!r} not found")

    def delete_operator(self, username: str, force: bool = False) -> None:
        """Remove an operator account and every session it holds.

        Replies they wrote stay on their tickets: `comments.author_name` is
        stored as text rather than a reference, so removing the account never
        rewrites history.
        """
        username = username.strip().lower()
        row = self.conn.execute(
            "SELECT id FROM operators WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            raise NotFound(f"operator {username!r} not found")
        total = self.conn.execute("SELECT COUNT(*) AS n FROM operators").fetchone()["n"]
        if total <= 1 and not force:
            raise ServiceError(
                "last_operator",
                "that is the only operator; removing it locks you out of the web UI"
                " (pass force=True if you mean it)",
                409,
            )
        with self._write_lock:
            # sessions.operator_id is ON DELETE CASCADE, so this signs them out.
            self.conn.execute("DELETE FROM operators WHERE id=?", (row["id"],))
        self._log_event(None, None, "operator", "operator.deleted", {"username": username})

    def list_operators(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM operators ORDER BY username")]

    def has_operators(self) -> bool:
        return self.conn.execute("SELECT 1 FROM operators LIMIT 1").fetchone() is not None

    def login(
        self, username: str, password: str, user_agent: str | None = None, ttl_days: int = 14
    ) -> tuple[str, str]:
        """Verify credentials and open a session. Returns (cookie, csrf_token)."""
        row = self.conn.execute(
            "SELECT * FROM operators WHERE username=?", (username.strip().lower(),)
        ).fetchone()
        # Always run the KDF, even for an unknown user, so response time does
        # not reveal which usernames exist.
        stored = row["password_hash"] if row else auth.DUMMY_PASSWORD_HASH
        valid = auth.verify_password(password, stored)
        if row is None or not valid:
            raise ServiceError("invalid_login", "username or password is incorrect", 401)

        cred = security.generate_credential(
            security.SESSION_PREFIX, self.settings.env, self.settings.secret
        )
        csrf = auth.new_csrf_token()
        stamp = now()
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO sessions(id, operator_id, secret_hash, csrf_token, created_at,"
                " expires_at, last_seen_at, user_agent) VALUES(?,?,?,?,?,?,?,?)",
                (
                    cred.key_id,
                    row["id"],
                    cred.secret_hash,
                    csrf,
                    iso(stamp),
                    iso(stamp + timedelta(days=ttl_days)),
                    iso(stamp),
                    (user_agent or "")[:300],
                ),
            )
            self.conn.execute(
                "UPDATE operators SET last_login_at=? WHERE id=?", (iso(stamp), row["id"])
            )
        self._log_event(None, None, row["username"], "operator.login", {})
        return cred.token, csrf

    def authenticate_session(self, cookie: str) -> auth.OperatorPrincipal:
        parsed = security.parse_token(cookie)
        if parsed is None or parsed.prefix != security.SESSION_PREFIX:
            raise ServiceError("invalid_session", "not signed in", 401)
        row = self.conn.execute(
            "SELECT s.*, o.username, o.display_name FROM sessions s"
            " JOIN operators o ON o.id=s.operator_id WHERE s.id=?",
            (parsed.key_id,),
        ).fetchone()
        expected = row["secret_hash"] if row else "0" * 64
        valid = security.verify_secret(parsed.secret, expected, self.settings.secret)
        if row is None or not valid:
            raise ServiceError("invalid_session", "not signed in", 401)
        if row["revoked_at"]:
            raise ServiceError("invalid_session", "session has been revoked", 401)
        if parse_iso(row["expires_at"]) < now():
            raise ServiceError("invalid_session", "session has expired", 401)

        with self._write_lock:
            self.conn.execute(
                "UPDATE sessions SET last_seen_at=? WHERE id=?", (iso(now()), row["id"])
            )
        return auth.OperatorPrincipal(
            operator_id=row["operator_id"],
            username=row["username"],
            display_name=row["display_name"] or row["username"],
            session_id=row["id"],
            csrf_token=row["csrf_token"],
        )

    def logout(self, session_id: str) -> None:
        with self._write_lock:
            self.conn.execute(
                "UPDATE sessions SET revoked_at=? WHERE id=?", (iso(now()), session_id)
            )

    def revoke_operator_sessions(self, username: str) -> int:
        with self._write_lock:
            cur = self.conn.execute(
                "UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL AND operator_id ="
                " (SELECT id FROM operators WHERE username=?)",
                (iso(now()), username.strip().lower()),
            )
        return cur.rowcount

    def purge_expired_sessions(self) -> int:
        with self._write_lock:
            cur = self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(now()),))
        return cur.rowcount

    # --------------------------------------------------------------- retention

    def purge_candidates(
        self,
        *,
        older_than_days: int,
        statuses: Sequence[str] = (TicketStatus.CLOSED, TicketStatus.RESOLVED),
        client_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Tickets whose files a retention sweep would strip.

        Age is measured from when the ticket was last touched, not created: a
        ticket closed yesterday after a six-month argument is not old.
        """
        cutoff = iso(now() - timedelta(days=older_than_days))
        sql = (
            "SELECT t.id, t.ref, t.title, t.status, t.updated_at, c.slug AS client_slug,"
            " COUNT(a.id) AS file_count, COALESCE(SUM(a.size_bytes), 0) AS bytes"
            " FROM tickets t JOIN clients c ON c.id=t.client_id"
            " JOIN attachments a ON a.ticket_id=t.id"
            " WHERE t.updated_at < ?"
        )
        params: list[Any] = [cutoff]
        if statuses:
            sql += f" AND t.status IN ({','.join('?' * len(statuses))})"
            params.extend(str(s) for s in statuses)
        if client_id:
            sql += " AND t.client_id=?"
            params.append(client_id)
        sql += " GROUP BY t.id ORDER BY bytes DESC"
        return [dict(row) for row in self.conn.execute(sql, params)]

    def purge_attachments(self, ticket_ids: Sequence[str]) -> dict[str, int]:
        """Strip files from tickets, keeping the tickets themselves.

        Screenshots are effectively all the disk usage, so this reclaims the
        space while leaving the ticket, its context and its conversation intact
        and searchable.
        """
        freed_bytes = freed_files = touched = 0
        for ticket_id in ticket_ids:
            rows = list(
                self.conn.execute(
                    "SELECT id, sha256, size_bytes FROM attachments WHERE ticket_id=?",
                    (ticket_id,),
                )
            )
            if not rows:
                continue
            digests = {row["sha256"] for row in rows}
            size = sum(row["size_bytes"] for row in rows)
            with self._transaction():
                self.conn.execute("DELETE FROM attachments WHERE ticket_id=?", (ticket_id,))
                self.conn.execute(
                    "UPDATE tickets SET purged_at=?, purged_files=purged_files+?,"
                    " purged_bytes=purged_bytes+? WHERE id=?",
                    (iso(now()), len(rows), size, ticket_id),
                )
            # Blobs are content-addressed and shared, so only drop one once the
            # last attachment row pointing at it is gone.
            for digest in digests:
                still_used = self.conn.execute(
                    "SELECT 1 FROM attachments WHERE sha256=? LIMIT 1", (digest,)
                ).fetchone()
                if still_used is None:
                    path = self.blobs.path_for(digest)
                    if path.exists():
                        freed_bytes += path.stat().st_size
                    self.blobs.delete_if_unreferenced(digest, still_referenced=False)
            freed_files += len(rows)
            touched += 1
            self._log_event(
                ticket_id, None, "operator", "ticket.purged", {"files": len(rows), "bytes": size}
            )
        return {"tickets": touched, "files": freed_files, "bytes_freed": freed_bytes}

    def collect_orphan_blobs(self, dry_run: bool = True) -> dict[str, int]:
        """Remove blobs no attachment references.

        A rolled-back create can leave one behind, since blobs are written
        before the transaction opens.
        """
        referenced = {
            row["sha256"] for row in self.conn.execute("SELECT DISTINCT sha256 FROM attachments")
        }
        count = freed = 0
        for path in self.blobs.root.rglob("*"):
            if not path.is_file() or path.name in referenced:
                continue
            count += 1
            freed += path.stat().st_size
            if not dry_run:
                path.unlink(missing_ok=True)
        return {"blobs": count, "bytes": freed}

    def storage_usage(self) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes FROM attachments"
        ).fetchone()
        on_disk = sum(p.stat().st_size for p in self.blobs.root.rglob("*") if p.is_file())
        return {
            "attachment_rows": row["files"],
            "logical_bytes": row["bytes"],
            "disk_bytes": on_disk,
        }

    # ----------------------------------------------------------------- events

    def _log_event(
        self,
        ticket_id: str | None,
        client_id: str | None,
        actor: str,
        kind: str,
        data: dict[str, Any],
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                "INSERT INTO events(ticket_id, client_id, actor, kind, data_json, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (ticket_id, client_id, actor, kind, json.dumps(data, default=str), iso(now())),
            )
