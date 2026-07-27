"""Pydantic request/response models — the wire contract for the Odoo module.

The field names and enum values here are the source of truth for docs/API.md
and the generated OpenAPI document.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TicketStatus(StrEnum):
    NEW = "new"
    OPEN = "open"
    WAITING_CLIENT = "waiting_client"
    WAITING_THIRD_PARTY = "waiting_third_party"
    RESOLVED = "resolved"
    CLOSED = "closed"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class Category(StrEnum):
    BUG = "bug"
    ERROR = "error"
    QUESTION = "question"
    HOW_TO = "how_to"
    DATA = "data"
    PERFORMANCE = "performance"
    FEATURE_REQUEST = "feature_request"
    ACCESS = "access"
    INTEGRATION = "integration"
    OTHER = "other"


class Source(StrEnum):
    ODOO_SERVER = "odoo_server"
    """Relayed by the client's Odoo backend using the client's API key."""
    ODOO_BROWSER = "odoo_browser"
    """Uploaded straight from the browser using a single-use upload token."""
    API = "api"
    MANUAL = "manual"
    EMAIL = "email"


# Statuses a client may set on its own tickets. Everything else is the
# operator's to decide, so a client cannot mark its own ticket resolved.
CLIENT_SETTABLE_STATUSES = {TicketStatus.CLOSED, TicketStatus.OPEN}

ShortText = Annotated[str, Field(min_length=1, max_length=200)]
LongText = Annotated[str, Field(max_length=20000)]


class Reporter(BaseModel):
    """Who hit the problem, as known to the client's Odoo instance."""

    model_config = ConfigDict(extra="forbid")

    name: ShortText
    email: Annotated[str | None, Field(max_length=200)] = None
    login: Annotated[str | None, Field(max_length=200)] = None
    odoo_uid: int | None = None
    phone: Annotated[str | None, Field(max_length=50)] = None


class ErrorInfo(BaseModel):
    """The Odoo/JS error that triggered the report, when there was one."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(max_length=300)] = None
    message: Annotated[str | None, Field(max_length=4000)] = None
    traceback: Annotated[str | None, Field(max_length=60000)] = None
    server_traceback: Annotated[str | None, Field(max_length=60000)] = None
    rpc_route: Annotated[str | None, Field(max_length=500)] = None
    rpc_model: Annotated[str | None, Field(max_length=200)] = None
    rpc_method: Annotated[str | None, Field(max_length=200)] = None
    http_status: int | None = None


class BrowserInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_agent: Annotated[str | None, Field(max_length=500)] = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    device_pixel_ratio: float | None = None
    language: Annotated[str | None, Field(max_length=20)] = None


class TicketContext(BaseModel):
    """Everything about *where* the user was when the problem happened.

    All fields are optional so a module can send what it can get, but the more
    of this that arrives, the less back-and-forth a ticket needs.
    """

    model_config = ConfigDict(extra="allow")

    url: Annotated[str | None, Field(max_length=2000)] = None
    page_title: Annotated[str | None, Field(max_length=500)] = None

    odoo_version: Annotated[str | None, Field(max_length=50)] = None
    database: Annotated[str | None, Field(max_length=100)] = None
    company_id: int | None = None
    company_name: Annotated[str | None, Field(max_length=200)] = None

    model: Annotated[str | None, Field(max_length=200)] = None
    res_id: int | None = None
    view_type: Annotated[str | None, Field(max_length=50)] = None
    action_id: int | None = None
    action_xml_id: Annotated[str | None, Field(max_length=200)] = None
    menu_xml_id: Annotated[str | None, Field(max_length=200)] = None

    user_lang: Annotated[str | None, Field(max_length=20)] = None
    user_tz: Annotated[str | None, Field(max_length=64)] = None
    client_timestamp: datetime | None = None

    installed_modules: Annotated[list[str] | None, Field(max_length=500)] = None
    error: ErrorInfo | None = None
    browser: BrowserInfo | None = None


class InlineFile(BaseModel):
    """A file sent inside a JSON body as base64 (or a `data:` URI)."""

    model_config = ConfigDict(extra="forbid")

    filename: Annotated[str, Field(max_length=255)] = "attachment"
    content_type: Annotated[str | None, Field(max_length=100)] = None
    data: str = Field(description="Base64 payload, optionally a full data: URI")
    caption: Annotated[str | None, Field(max_length=300)] = None


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: ShortText
    description: LongText = ""
    reporter: Reporter | None = None
    """Required for API-key auth; ignored for upload tokens, which carry a
    reporter identity bound at mint time by the client's Odoo backend."""

    priority: Priority = Priority.NORMAL
    category: Category = Category.OTHER
    tags: Annotated[list[Annotated[str, Field(max_length=50)]], Field(max_length=20)] = []
    external_ref: Annotated[str | None, Field(max_length=100)] = None
    context: TicketContext = Field(default_factory=TicketContext)
    screenshot: InlineFile | None = None
    attachments: Annotated[list[InlineFile], Field(max_length=10)] = []

    @field_validator("tags")
    @classmethod
    def _strip_tags(cls, tags: list[str]) -> list[str]:
        return [t.strip() for t in tags if t.strip()]


class TicketUpdate(BaseModel):
    """Client-side edits. The operator has a wider set of fields via the TUI."""

    model_config = ConfigDict(extra="forbid")

    title: ShortText | None = None
    description: LongText | None = None
    priority: Priority | None = None
    category: Category | None = None
    status: Literal["open", "closed"] | None = None
    tags: Annotated[list[str], Field(max_length=20)] | None = None
    external_ref: Annotated[str | None, Field(max_length=100)] = None


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, Field(min_length=1, max_length=20000)]
    author: Reporter | None = None
    attachments: Annotated[list[InlineFile], Field(max_length=10)] = []


class AttachmentOut(BaseModel):
    id: str
    role: str
    filename: str
    content_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    created_at: datetime
    download_url: str


class CommentOut(BaseModel):
    id: str
    author_type: str
    author_name: str
    body: str
    created_at: datetime
    attachments: list[AttachmentOut] = []


class TicketOut(BaseModel):
    id: str
    ref: str
    title: str
    description: str
    status: TicketStatus
    priority: Priority
    category: Category
    source: Source
    tags: list[str] = []
    external_ref: str | None = None
    reporter: Reporter | None = None
    context: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    attachments: list[AttachmentOut] = []
    """Files attached to the ticket itself. Files sent with a reply live on
    that comment instead, so nothing is listed twice."""
    comments: list[CommentOut] | None = None
    comment_count: int = 0
    comments_truncated: bool = False
    """True when `comments` holds only the most recent slice; fetch the rest
    from `GET /tickets/{id}/comments`."""


class CommentList(BaseModel):
    items: list[CommentOut]
    total: int
    has_more: bool = False


class TicketList(BaseModel):
    items: list[TicketOut]
    next_cursor: str | None = None
    """Position to resume from. In sync mode (`updated_since` or a sync cursor)
    this is always present — persist it and pass it back as `cursor`. In inbox
    mode it is only set while `has_more` is true."""
    has_more: bool = False


class UploadTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reporter: Reporter
    ttl_seconds: Annotated[int | None, Field(ge=30, le=3600)] = None


class UploadTokenOut(BaseModel):
    token: str
    expires_at: datetime
    max_file_bytes: int
    max_files: int


class WhoAmI(BaseModel):
    client_id: str
    client_slug: str
    client_name: str
    api_key_id: str
    api_key_name: str
    auth_type: Literal["api_key", "upload_token"]
    server_time: datetime


class ErrorOut(BaseModel):
    error: str
    """Stable machine-readable code, e.g. `invalid_api_key`."""
    message: str
    detail: Any | None = None
