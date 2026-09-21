"""The storage contract every backend implements.

Two methods, ``save`` and ``read``, and that is the whole interface the rest of
the platform is allowed to depend on. Keeping it this small is what made adding
an S3 backend a new file rather than a refactor: `persistence.py` and the
artifact endpoint in `main.py` call these two methods and nothing else, so they
did not change at all.

Both backends are **content-addressed**: the key is derived from a SHA-256 of
the bytes, so the same screenshot captured by two different checks is stored
once, and a key is never attacker-influenced. That property is why neither
backend has to sanitise a caller-supplied path - there is no such thing here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class ArtifactNotFound(FileNotFoundError):
    """No object exists at this storage key."""


@dataclass(frozen=True)
class StoredArtifact:
    storage_key: str
    size_bytes: int


class ArtifactStore(Protocol):
    """What `persistence.py` depends on. Nothing more may be added lightly:
    every method here is one a future backend has to implement."""

    def save(
        self, data: bytes, *, org_id: UUID | str, kind: str, extension: str
    ) -> StoredArtifact: ...

    def read(self, storage_key: str) -> bytes: ...


def content_key(data: bytes, *, org_id: UUID | str, kind: str, extension: str) -> str:
    """``<org>/<kind>/<sha256><ext>`` - the same layout in every backend.

    Shared so a deployment can migrate a filesystem root into a bucket with a
    plain recursive copy, and so an artifact written by one backend is findable
    by the other. Two backends inventing two layouts would make that a
    migration script instead of a `cp`.

    The org prefix is a filing convenience, not access control: nothing in the
    storage layer re-checks tenancy. Callers reach a key either by having just
    written it, or through an `Artifact` row that Postgres RLS (ADR-0007) has
    already filtered to their tenant.
    """
    digest = hashlib.sha256(data).hexdigest()
    return f"{org_id}/{kind}/{digest}{extension}"
