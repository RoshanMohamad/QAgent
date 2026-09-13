"""Password hashing and JWT issuance.

`main.current_org` used to trust a raw X-Org-Id header as a stand-in for real
authentication. This module is what replaces the stand-in: bcrypt for at-rest
password storage, HS256 JWTs signed with `settings.secret_key` for bearer
tokens. It has no FastAPI or database dependency so it can be unit-tested on
its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import jwt

ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(hours=12)


class AuthError(Exception):
    """Invalid credentials or an invalid/expired token."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash (e.g. a row that predates hashing). Never a match.
        return False


def create_access_token(*, user_id: UUID, org_id: UUID, role: str, secret_key: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "role": role,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
    }
    return jwt.encode(claims, secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str, secret_key: str) -> dict:
    try:
        return jwt.decode(token, secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise AuthError("invalid or expired token") from exc
