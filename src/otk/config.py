"""Runtime configuration, read from the environment with usable defaults.

Everything the server needs lives under a single data directory so that a
deployment is one directory to back up: the SQLite database, the attachment
blobs, and the server-side pepper used to hash API keys.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "odoo-tickets"

# Attachments are stored on disk, but the request itself is buffered in memory,
# so these caps are what stand between a client and the server's RAM.
DEFAULT_MAX_FILE_MB = 10
DEFAULT_MAX_TICKET_MB = 25
# The whole request. Base64 inflates payloads ~33%, so this sits above the
# attachment cap with room for the JSON around it.
DEFAULT_MAX_BODY_MB = 36
DEFAULT_MAX_FILES = 10


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Every field past `secret` has a default.

    That is deliberate: callers construct this directly (tests, embedding), and
    a new setting should not break every existing call site.
    """

    data_dir: Path
    secret: str
    env: str = "live"
    host: str = "127.0.0.1"
    port: int = 8787
    web_port: int = 8788
    max_file_bytes: int = DEFAULT_MAX_FILE_MB * 1024 * 1024
    max_ticket_bytes: int = DEFAULT_MAX_TICKET_MB * 1024 * 1024
    max_body_bytes: int = DEFAULT_MAX_BODY_MB * 1024 * 1024
    max_files_per_ticket: int = DEFAULT_MAX_FILES
    upload_token_ttl_seconds: int = 600
    rate_limit_per_minute: int = 60
    cors_origins: tuple[str, ...] = ()
    retention_days: int = 0
    """Age past which a closed ticket's files are purged by `otk purge --auto`.
    0 disables automatic retention; manual purging still works."""
    display_tz: str = ""
    """IANA zone the UI renders timestamps in, e.g. `Europe/Lisbon`.

    Empty means the server's own zone, which is almost never what you want:
    the host and the person reading the screen are usually in different
    countries, and an hour's silent offset is worse than an obviously wrong
    one. Everything is stored in UTC regardless."""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tickets.db"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"


def _load_secret(data_dir: Path) -> str:
    """Return the pepper used to hash API keys, generating it on first run.

    Rotating this file invalidates every issued key, so it is created once and
    left alone. It is kept out of the database on purpose: a leaked database
    backup alone must not be enough to authenticate as a client.
    """
    from_env = os.environ.get("OTK_SECRET", "").strip()
    if from_env:
        return from_env

    secret_file = data_dir / "secret.key"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()

    data_dir.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_hex(32)
    # Create with 0600 from the start rather than chmod-ing after writing,
    # which would leave a window where the pepper is world-readable.
    fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(generated)
    return generated


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("OTK_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    raw_origins = os.environ.get("OTK_CORS_ORIGINS", "").strip()
    origins = tuple(o.strip() for o in raw_origins.split(",") if o.strip())

    return Settings(
        data_dir=data_dir,
        env=os.environ.get("OTK_ENV", "live"),
        secret=_load_secret(data_dir),
        host=os.environ.get("OTK_HOST", "127.0.0.1"),
        port=_env_int("OTK_PORT", 8787),
        web_port=_env_int("OTK_WEB_PORT", 8788),
        max_file_bytes=_env_int("OTK_MAX_FILE_MB", DEFAULT_MAX_FILE_MB) * 1024 * 1024,
        max_ticket_bytes=_env_int("OTK_MAX_TICKET_MB", DEFAULT_MAX_TICKET_MB) * 1024 * 1024,
        max_body_bytes=_env_int("OTK_MAX_BODY_MB", DEFAULT_MAX_BODY_MB) * 1024 * 1024,
        max_files_per_ticket=_env_int("OTK_MAX_FILES", DEFAULT_MAX_FILES),
        upload_token_ttl_seconds=_env_int("OTK_UPLOAD_TOKEN_TTL", 600),
        rate_limit_per_minute=_env_int("OTK_RATE_LIMIT", 60),
        cors_origins=origins,
        retention_days=_env_int("OTK_RETENTION_DAYS", 0),
        display_tz=os.environ.get("OTK_TZ", "").strip(),
    )


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests that repoint OTK_DATA_DIR."""
    get_settings.cache_clear()
