"""Local-filesystem object storage for evidence artifacts (CLAUDE.md section 15).

A bug report that says "the page threw" is a claim the reader has to take on
faith; a bug report with the actual screenshot and console log attached is
evidence. The `Artifact` table (models.py) has existed since the schema was
first written to be that attachment point, but nothing wrote to it - this is
the first backend that does.

This is deliberately the simplest thing that could work: a directory on disk,
one file per distinct set of bytes, named by content hash so identical evidence
(the same screenshot captured by two different checks) is stored once. The
README's own stack table lists S3-compatible storage / Cloudflare R2 as the
eventual backend for a real deployment - nothing here forecloses that. `save`
and `read` are the entire contract every caller depends on (persistence.py is
the only caller), so swapping the implementation later touches this file and
the one line that constructs it, not every call site.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

logger = logging.getLogger(__name__)


class ArtifactNotFound(FileNotFoundError):
    """No object exists at this storage key."""


@dataclass(frozen=True)
class StoredArtifact:
    storage_key: str
    size_bytes: int


class LocalArtifactStore:
    """Files under ``root``, scoped ``org_id/kind/<sha256><extension>``.

    The tenant scoping in the path is a filing convenience, not the access
    control: nothing here re-checks ``org_id`` on read. The only caller,
    `persistence.py`, only ever reads a key it just wrote for that same
    org, or one already reached through an `Artifact` row that Postgres RLS
    (ADR-0007) has already filtered to the caller's tenant.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self, data: bytes, *, org_id: UUID | str, kind: str, extension: str
    ) -> StoredArtifact:
        digest = hashlib.sha256(data).hexdigest()
        relative = Path(str(org_id)) / kind / f"{digest}{extension}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():  # content-addressed: identical bytes, one file on disk
            path.write_bytes(data)
        return StoredArtifact(storage_key=relative.as_posix(), size_bytes=len(data))

    def read(self, storage_key: str) -> bytes:
        path = self._resolve(storage_key)
        if not path.is_file():
            raise ArtifactNotFound(storage_key)
        return path.read_bytes()

    def _resolve(self, storage_key: str) -> Path:
        """Reject a key that would resolve outside ``root``.

        Defence in depth: every key reaching this method should already be one
        `save` produced (a content hash, never attacker-influenced), but a
        traversal payload is cheap to rule out and expensive to regret.
        """
        root = self.root.resolve()
        path = (root / storage_key).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"storage key escapes artifact root: {storage_key!r}")
        return path


def store_from_settings(settings=None) -> LocalArtifactStore:
    if settings is None:
        from qagent.config import get_settings

        settings = get_settings()
    return LocalArtifactStore(settings.qagent_artifact_root)
