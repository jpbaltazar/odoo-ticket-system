"""Translate internal records into the public wire shapes."""

from __future__ import annotations

import mimetypes

from ..schemas import (
    AttachmentOut,
    CommentOut,
    InlineFile,
    Reporter,
    TicketOut,
)
from ..service import AttachmentRecord, CommentRecord, IncomingFile, ServiceError, TicketRecord
from ..storage import StorageError, decode_payload, content_type_from_data_uri

ATTACHMENT_URL = "/api/v1/attachments/{id}"


def attachment_out(record: AttachmentRecord) -> AttachmentOut:
    return AttachmentOut(
        id=record.id,
        role=record.role,
        filename=record.filename,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        width=record.width,
        height=record.height,
        created_at=record.created_at,
        download_url=ATTACHMENT_URL.format(id=record.id),
    )


def comment_out(record: CommentRecord) -> CommentOut:
    return CommentOut(
        id=record.id,
        author_type=record.author_type,
        author_name=record.author_name,
        body=record.body,
        created_at=record.created_at,
        attachments=[attachment_out(a) for a in record.attachments],
    )


def ticket_out(record: TicketRecord, include_comments: bool = True) -> TicketOut:
    reporter = None
    if record.reporter_name:
        reporter = Reporter(
            name=record.reporter_name,
            email=record.reporter.get("email"),
            login=record.reporter.get("login"),
            odoo_uid=record.reporter.get("odoo_uid"),
        )
    return TicketOut(
        id=record.id,
        ref=record.ref,
        title=record.title,
        description=record.description,
        status=record.status,
        priority=record.priority,
        category=record.category,
        source=record.source,
        tags=record.tags,
        external_ref=record.external_ref,
        reporter=reporter,
        context=record.context,
        created_at=record.created_at,
        updated_at=record.updated_at,
        resolved_at=record.resolved_at,
        closed_at=record.closed_at,
        attachments=[attachment_out(a) for a in record.attachments],
        comments=[comment_out(c) for c in record.comments] if include_comments else None,
        comment_count=record.comment_count,
        comments_truncated=record.comments_truncated if include_comments else False,
    )


def _resolve_content_type(inline: InlineFile) -> str:
    if inline.content_type:
        return inline.content_type
    from_uri = content_type_from_data_uri(inline.data)
    if from_uri:
        return from_uri
    guessed, _ = mimetypes.guess_type(inline.filename)
    return guessed or "application/octet-stream"


def incoming_from_inline(inline: InlineFile, role: str = "attachment") -> IncomingFile:
    try:
        payload = decode_payload(inline.data)
    except StorageError as exc:
        raise ServiceError("invalid_attachment", f"{inline.filename}: {exc}") from exc
    return IncomingFile(
        data=payload,
        filename=inline.filename,
        content_type=_resolve_content_type(inline),
        role=role,
    )
