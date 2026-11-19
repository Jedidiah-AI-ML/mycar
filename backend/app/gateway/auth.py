"""Device credentials: the upload token and the claim code on the device's label.

**Device token** = `<device_id>.<secret>`, sent as `Authorization: Bearer ...` (ADR-006).
The secret is 32 random bytes (256 bits). We store only `sha256(secret)`: with that much
randomness nobody can guess it from the hash, so a slow password hash isn't needed, and
checking it on every upload stays cheap. Comparison is constant-time.

**Claim code** = 8 characters like `K7QM-3XPA`, printed on the label (QR) so an owner can
link the device to their car. It's short enough to type, so about 40 bits: a plain hash
could be brute-forced from a leaked database. We store an **HMAC** of it instead, keyed
with a server-side secret derived from `JWT_SECRET`, so the stored value is useless
without the key. (Rotating `JWT_SECRET` invalidates unclaimed claim codes; re-provision.)
"""

import hashlib
import hmac
import re
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Device

# No 0/O, 1/I/L, U: easy to read off a label and type on a phone.
CLAIM_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CLAIM_LEN = 8
_TOKEN_RE = re.compile(r"^(?P<id>[A-Z0-9][A-Z0-9-]{2,31})\.(?P<secret>[A-Za-z0-9_-]{32,128})$")
_DUMMY_HASH = hashlib.sha256(b"timing-equaliser").hexdigest()


def new_device_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def device_token(device_id: str, secret: str) -> str:
    return f"{device_id}.{secret}"


def parse_token(token: str) -> tuple[str, str] | None:
    m = _TOKEN_RE.match(token.strip())
    return (m["id"], m["secret"]) if m else None


async def authenticate_device(session: AsyncSession, authorization: str | None) -> Device | None:
    """The device for a `Bearer <device_id>.<secret>` header, or None (caller sends 401).

    Unknown device, wrong secret and disabled device all take the same path and time.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    parsed = parse_token(authorization.removeprefix("Bearer "))
    if parsed is None:
        return None
    device_id, secret = parsed
    device = await session.get(Device, device_id)
    stored = device.token_hash if device is not None else _DUMMY_HASH
    ok = hmac.compare_digest(hash_secret(secret), stored)
    if device is None or not ok or device.disabled_at is not None:
        return None
    return device


# ---------------------------------------------------------------- claim codes


def new_claim_code() -> str:
    raw = "".join(secrets.choice(CLAIM_ALPHABET) for _ in range(CLAIM_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def normalise_claim_code(code: str) -> str:
    """Forgiving input: case, spaces and dashes don't matter."""
    return re.sub(r"[\s-]", "", code).upper()


def _claim_key() -> bytes:
    jwt = get_settings().jwt_secret.get_secret_value()
    return hashlib.sha256(b"autocare-claim-code-v1:" + jwt.encode()).digest()


def hash_claim_code(code: str) -> str:
    return hmac.new(_claim_key(), normalise_claim_code(code).encode(), hashlib.sha256).hexdigest()


def claim_code_ok(device: Device | None, code: str) -> bool:
    stored = device.claim_code_hash if device is not None and device.claim_code_hash else None
    ok = hmac.compare_digest(hash_claim_code(code), stored or _DUMMY_HASH)
    return stored is not None and ok
