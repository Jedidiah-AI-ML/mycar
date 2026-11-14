"""Passwords, access tokens and refresh tokens.

- Passwords: Argon2id (memory-hard, so GPU guessing is expensive).
- Access token: short-lived signed JWT; the API trusts it without a DB lookup.
- Refresh token: long random string. Only its SHA-256 is stored, so a database leak
  doesn't hand out working sessions. Each use swaps it for a new one ("rotation");
  presenting an already-used token means it was copied, so the user's sessions are
  all revoked ("reuse detection").
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import get_settings

_hasher = PasswordHash((Argon2Hasher(),))
# Verified against when the email doesn't exist, so a login takes the same time either
# way and can't be used to discover which emails have accounts.
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


class InvalidToken(Exception):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        _hasher.verify(password, _DUMMY_HASH)
        return False
    return _hasher.verify(password, password_hash)


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    s = get_settings()
    ttl = timedelta(minutes=s.jwt_access_ttl_minutes)
    now = datetime.now(UTC)
    claims = {"sub": str(user_id), "type": "access", "iat": now, "exp": now + ttl}
    token = jwt.encode(claims, s.jwt_secret.get_secret_value(), algorithm=s.jwt_algorithm)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> uuid.UUID:
    s = get_settings()
    try:
        claims = jwt.decode(
            token,
            s.jwt_secret.get_secret_value(),
            algorithms=[s.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
        if claims.get("type") != "access":
            raise InvalidToken("wrong token type")
        return uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidToken(str(exc)) from exc


def new_refresh_token() -> tuple[str, str]:
    """Returns (raw token for the client, sha256 hex for the database)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def refresh_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=get_settings().jwt_refresh_ttl_days)
