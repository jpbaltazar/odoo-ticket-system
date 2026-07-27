"""Operator authentication: passwords and server-side sessions.

Deliberately separate from `security.py`, which deals in machine credentials.
Those are 256-bit random secrets where a keyed hash is sufficient; a password
is human-chosen and low-entropy, so it needs a deliberately slow KDF.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

# scrypt parameters. n=2^15 costs roughly 100ms and 32 MB per verification,
# which is negligible for a login page and expensive for an offline cracker.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2

MIN_PASSWORD_LENGTH = 12


class WeakPassword(ValueError):
    """Raised when a proposed password fails the minimum policy."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )


def hash_password(password: str) -> str:
    """Return a self-describing hash: the parameters travel with the digest.

    Storing n/r/p means these can be raised later without invalidating
    existing passwords — an old hash still verifies under its own settings.
    """
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        salt_bytes, expected = _unb64(salt), _unb64(digest)
    except (ValueError, TypeError):
        return False

    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
        maxmem=128 * n * r * 2,
    )
    return hmac.compare_digest(candidate, expected)


# A dummy hash to verify against when the username does not exist, so a login
# attempt costs the same either way and cannot be used to enumerate accounts.
DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-constant-time-login")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class OperatorPrincipal:
    """The signed-in human behind a web request."""

    operator_id: str
    username: str
    display_name: str
    session_id: str
    csrf_token: str
