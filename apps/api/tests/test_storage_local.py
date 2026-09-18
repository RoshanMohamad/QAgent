"""LocalArtifactStore: the first real backend for the Artifact table (CLAUDE.md
section 15), tested entirely against a temp directory - no network, no cloud
credentials, matching README's "everything runs green without a database."
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from qagent.modules.storage.local import ArtifactNotFound, LocalArtifactStore


def test_save_then_read_round_trips(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    org_id = uuid.uuid4()

    stored = store.save(b"hello", org_id=org_id, kind="log", extension=".log")

    assert store.read(stored.storage_key) == b"hello"
    assert stored.size_bytes == 5


def test_identical_bytes_are_stored_once(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    org_id = uuid.uuid4()

    first = store.save(b"same bytes", org_id=org_id, kind="screenshot", extension=".png")
    second = store.save(b"same bytes", org_id=org_id, kind="screenshot", extension=".png")

    assert first.storage_key == second.storage_key
    # Only one file on disk for it, not two.
    matches = list(tmp_path.rglob("*.png"))
    assert len(matches) == 1


def test_different_orgs_get_different_keys_for_the_same_bytes(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)

    a = store.save(b"same bytes", org_id=uuid.uuid4(), kind="log", extension=".log")
    b = store.save(b"same bytes", org_id=uuid.uuid4(), kind="log", extension=".log")

    assert a.storage_key != b.storage_key


def test_read_missing_key_raises_artifact_not_found(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(ArtifactNotFound):
        store.read("does/not/exist.png")


def test_read_rejects_path_traversal(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(ValueError, match="escapes artifact root"):
        store.read("../../etc/passwd")


def test_root_directory_is_created_if_missing(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist" / "yet"
    store = LocalArtifactStore(root)

    assert root.is_dir()
    stored = store.save(b"x", org_id=uuid.uuid4(), kind="log", extension=".log")
    assert store.read(stored.storage_key) == b"x"
