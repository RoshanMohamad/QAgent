"""Password hashing and JWT issuance (qagent.modules.auth.security)."""

from __future__ import annotations

import time
import uuid

import jwt
import pytest

from qagent.modules.auth.security import (
    AuthError,
    create_access_token,
    decode_access_token,
    has_role,
    hash_password,
    verify_password,
)

SECRET = "test-secret"


def test_hash_password_does_not_store_plaintext() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed)


def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", hashed)


def test_verify_password_rejects_malformed_hash() -> None:
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_access_token_round_trips_claims() -> None:
    user_id, org_id = uuid.uuid4(), uuid.uuid4()
    token = create_access_token(user_id=user_id, org_id=org_id, role="owner", secret_key=SECRET)

    claims = decode_access_token(token, SECRET)
    assert claims["sub"] == str(user_id)
    assert claims["org_id"] == str(org_id)
    assert claims["role"] == "owner"


def test_decode_rejects_wrong_secret() -> None:
    token = create_access_token(
        user_id=uuid.uuid4(), org_id=uuid.uuid4(), role="member", secret_key=SECRET
    )
    with pytest.raises(AuthError):
        decode_access_token(token, "a-different-secret")


def test_decode_rejects_expired_token() -> None:
    expired = jwt.encode(
        {"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "role": "member", "exp": time.time() - 1},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        decode_access_token(expired, SECRET)


def test_has_role_owner_satisfies_member_requirement() -> None:
    assert has_role("owner", at_least="member") is True


def test_has_role_member_does_not_satisfy_owner_requirement() -> None:
    assert has_role("member", at_least="owner") is False


def test_has_role_same_role_satisfies_itself() -> None:
    assert has_role("member", at_least="member") is True
    assert has_role("owner", at_least="owner") is True


def test_has_role_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        has_role("superadmin", at_least="member")


def test_has_role_rejects_unknown_requirement() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        has_role("member", at_least="superadmin")
