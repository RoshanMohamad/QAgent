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
eventual backend for a real deployment.

That backend now exists (`s3.py`), and the claim this docstring used to make
turned out to be true: `save` and `read` were the entire contract, so adding it
meant a new file and a factory, with no change to `persistence.py` or to the
artifact endpoint at all. Both backends share the key layout from `base.py`, so
migrating a filesystem root into a bucket is a recursive copy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from qagent.modules.storage.base import ArtifactNotFound, StoredArtifact, content_key

logger = logging.getLogger(__name__)

# Re-exported: these moved to `base` when the S3 backend arrived and both
# backends needed them, and several call sites still import them from here.
__all__ = ["ArtifactNotFound", "LocalArtifactStore", "StoredArtifact", "store_from_settings"]


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
        key = content_key(data, org_id=org_id, kind=kind, extension=extension)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():  # content-addressed: identical bytes, one file on disk
            path.write_bytes(data)
        return StoredArtifact(storage_key=key, size_bytes=len(data))

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


def store_from_settings(settings=None):
    """Kept here because half the codebase imports it from this module.

    The real implementation lives in `factory.py`, which has to know about both
    backends; this module should not have to import S3 to hand back a
    filesystem store.
    """
    from qagent.modules.storage.factory import store_from_settings as build

    return build(settings)
