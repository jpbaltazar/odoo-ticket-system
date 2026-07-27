"""Credential generation and verification.

Two kinds of bearer credential exist, sharing one format:

    otk_<env>_<key_id>_<secret>    long-lived, one per client, server-side only
    ott_<env>_<key_id>_<secret>    single-use upload token, safe to hand a browser

The key id is stored in the clear so a lookup is a single indexed query; only
the secret half is hashed. Because secrets are 256 bits of CSPRNG output rather
than user-chosen passwords, a keyed HMAC-SHA256 is the right hash here — the
work factor of scrypt/argon2 buys nothing against an unguessable input, and
the pepper means a stolen database still cannot be used to forge a credential.
"""

from __future__ import annotations

import base64
import hmac
import os
from dataclasses import dataclass
from hashlib import sha256

API_KEY_PREFIX = "otk"
UPLOAD_TOKEN_PREFIX = "ott"
SESSION_PREFIX = "otks"
"""Operator web session. Never accepted on the `/api/v1` routes, and API keys
are never accepted as a session — the two audiences stay disjoint."""

_ALL_PREFIXES = (API_KEY_PREFIX, UPLOAD_TOKEN_PREFIX, SESSION_PREFIX)

_KEY_ID_BYTES = 6  # 48 bits, ample for an identifier
_SECRET_BYTES = 32  # 256 bits


def _b32(raw: bytes) -> str:
    """Lowercase unpadded base32: alphanumeric only, so `_` stays a separator."""
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


def generate_key_id() -> str:
    return _b32(os.urandom(_KEY_ID_BYTES))


def hash_secret(secret: str, pepper: str) -> str:
    return hmac.new(pepper.encode("utf-8"), secret.encode("utf-8"), sha256).hexdigest()


def verify_secret(secret: str, expected_hash: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_secret(secret, pepper), expected_hash)


@dataclass(frozen=True)
class GeneratedCredential:
    key_id: str
    secret_hash: str
    token: str
    """The full token. Shown once at creation and never recoverable afterwards."""


def generate_credential(prefix: str, env: str, pepper: str) -> GeneratedCredential:
    key_id = generate_key_id()
    secret = _b32(os.urandom(_SECRET_BYTES))
    return GeneratedCredential(
        key_id=key_id,
        secret_hash=hash_secret(secret, pepper),
        token=f"{prefix}_{env}_{key_id}_{secret}",
    )


@dataclass(frozen=True)
class ParsedToken:
    prefix: str
    env: str
    key_id: str
    secret: str


def parse_token(token: str) -> ParsedToken | None:
    """Split a bearer token into its parts, or return None if malformed."""
    parts = token.strip().split("_")
    if len(parts) != 4:
        return None
    prefix, env, key_id, secret = parts
    if prefix not in _ALL_PREFIXES:
        return None
    if not (env and key_id and secret):
        return None
    return ParsedToken(prefix=prefix, env=env, key_id=key_id, secret=secret)


def redact(token: str) -> str:
    """Render a token safe for logs: keeps the id, drops the secret."""
    parsed = parse_token(token)
    if parsed is None:
        return "<invalid>"
    return f"{parsed.prefix}_{parsed.env}_{parsed.key_id}_***"
