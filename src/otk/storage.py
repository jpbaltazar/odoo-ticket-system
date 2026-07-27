"""Content-addressed blob store for screenshots and attachments.

Files are keyed by SHA-256, so a client that reports the same screenshot twice
costs one copy on disk. Deletion is therefore refcounted against the
attachments table rather than done by path.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

DATA_URI_RE = re.compile(r"^data:(?P<mime>[\w.+/-]+)?(?:;charset=[\w-]+)?(?P<b64>;base64)?,")

# Deliberately narrow. Anything executable or scriptable is rejected outright
# rather than sniffed, since these files get opened on the operator's machine.
ALLOWED_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/zip": ".zip",
    "application/xml": ".xml",
    "text/xml": ".xml",
}

IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# Leading bytes that must match when the declared type is an image, so a
# client cannot park an arbitrary payload under an image/* label.
_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/pdf": (b"%PDF-",),
    "application/zip": (b"PK\x03\x04", b"PK\x05\x06"),
}


class StorageError(ValueError):
    """Raised when an attachment is unusable; surfaced to the caller as a 400."""


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size_bytes: int
    content_type: str
    filename: str
    width: int | None
    height: int | None


def decode_payload(data: str) -> bytes:
    """Decode a base64 string, tolerating a `data:` URI wrapper."""
    payload = data.strip()
    match = DATA_URI_RE.match(payload)
    if match:
        if not match.group("b64"):
            raise StorageError("data: URIs must be base64-encoded")
        payload = payload[match.end() :]
    payload = "".join(payload.split())
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise StorageError(f"attachment is not valid base64: {exc}") from exc


def content_type_from_data_uri(data: str) -> str | None:
    match = DATA_URI_RE.match(data.strip())
    return match.group("mime") if match else None


def sanitize_filename(name: str, content_type: str) -> str:
    """Reduce a client-supplied name to a safe basename with a sane extension."""
    base = Path(name.strip().replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(". ")
    if not base:
        base = "attachment"
    if len(base) > 120:
        stem, _, ext = base.rpartition(".")
        base = (stem[:100] or "attachment") + ("." + ext[:10] if ext else "")
    expected_ext = ALLOWED_CONTENT_TYPES.get(content_type)
    if expected_ext and not base.lower().endswith(expected_ext):
        base += expected_ext
    return base


def _check_magic(content_type: str, blob: bytes) -> None:
    signatures = _MAGIC.get(content_type)
    if not signatures:
        return
    if not any(blob.startswith(sig) for sig in signatures):
        raise StorageError(f"file contents do not match declared type {content_type}")


def _probe_image(blob: bytes) -> tuple[int | None, int | None]:
    """Return image dimensions, or (None, None) if it cannot be read."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(blob)) as img:
            return img.width, img.height
    except Exception:
        # Dimensions are a display nicety; a decode failure must not drop a ticket.
        return None, None


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        # Fan out on the first two hex chars so no directory grows unbounded.
        return self.root / digest[:2] / digest

    def put(
        self,
        blob: bytes,
        *,
        filename: str,
        content_type: str,
        max_bytes: int,
    ) -> StoredBlob:
        if not blob:
            raise StorageError("attachment is empty")
        if len(blob) > max_bytes:
            raise StorageError(
                f"attachment is {len(blob)} bytes, over the {max_bytes} byte limit"
            )

        normalized = (content_type or "").split(";")[0].strip().lower()
        if normalized not in ALLOWED_CONTENT_TYPES:
            raise StorageError(f"content type {normalized or '<missing>'} is not allowed")
        _check_magic(normalized, blob)

        digest = sha256(blob).hexdigest()
        target = self.path_for(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp name in the same directory, then rename, so a
            # crash mid-write cannot leave a truncated blob under a valid hash.
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(blob)
            tmp.replace(target)

        width = height = None
        if normalized in IMAGE_CONTENT_TYPES:
            width, height = _probe_image(blob)

        return StoredBlob(
            sha256=digest,
            size_bytes=len(blob),
            content_type=normalized,
            filename=sanitize_filename(filename, normalized),
            width=width,
            height=height,
        )

    def thumbnail(self, digest: str, content_type: str, max_edge: int = 480) -> bytes | None:
        """Return a cached WebP thumbnail, or None if this isn't a viewable image.

        The inbox shows one per ticket, so these are generated once and cached
        rather than re-encoding a 1280x720 PNG on every page load.
        """
        if content_type not in IMAGE_CONTENT_TYPES:
            return None
        cache = self.root.parent / "thumbs" / f"{digest}-{max_edge}.webp"
        if cache.exists():
            return cache.read_bytes()
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(self.read(digest))) as img:
                img = img.convert("RGB")
                img.thumbnail((max_edge, max_edge))
                buffer = io.BytesIO()
                img.save(buffer, format="WEBP", quality=80)
        except Exception:
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(buffer.getvalue())
        tmp.replace(cache)
        return buffer.getvalue()

    def read(self, digest: str) -> bytes:
        path = self.path_for(digest)
        if not path.exists():
            raise StorageError(f"blob {digest} is missing from the store")
        return path.read_bytes()

    def delete_if_unreferenced(self, digest: str, still_referenced: bool) -> None:
        if still_referenced:
            return
        self.path_for(digest).unlink(missing_ok=True)
