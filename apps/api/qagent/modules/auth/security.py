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

#: Every role that has ever existed, ranked. `register` mints the first user
#: of an organization as "owner"; every user after that is invited by an
#: owner (main.py's `POST /api/v1/users`) and defaults to "member". A role not
#: in this table is a bug, not a lower privilege - `has_role` raises rather
#: than silently denying, so a typo'd role string fails loudly instead of
#: quietly locking an org out of its own account.
ROLE_RANK = {"member": 0, "owner": 1}


class AuthError(Exception):
    """Invalid credentials or an invalid/expired token."""


def has_role(role: str, *, at_least: str) -> bool:
    """Is ``role`` at or above ``at_least`` in the hierarchy?

    A plain equality check (``role == "owner"``) would need updating at every
    call site the day a role is inserted between "member" and "owner"; ranking
    them once here means a new intermediate role only ever changes this table.
    """
    if role not in ROLE_RANK:
        raise ValueError(f"unknown role: {role!r}")
    if at_least not in ROLE_RANK:
        raise ValueError(f"unknown role: {at_least!r}")
    return ROLE_RANK[role] >= ROLE_RANK[at_least]


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
