"""Unit tests for artifact retention and eviction logic."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from qagent.models import Artifact
from qagent.modules.storage.retention import prune_artifacts


class _FakeStore:
    def __init__(self, existing_keys: set[str] | None = None) -> None:
        self.keys = set(existing_keys or [])
        self.deleted_keys: list[str] = []

    def delete(self, storage_key: str) -> bool:
        self.deleted_keys.append(storage_key)
        if storage_key in self.keys:
            self.keys.remove(storage_key)
            return True
        return False


class _FakeResult:
    def __init__(self, items: list[Any], scalar_val: Any = None) -> None:
        self._items = items
        self._scalar_val = scalar_val

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._items)

    def scalar_one(self) -> Any:
        return self._scalar_val if self._scalar_val is not None else len(self._items)


def test_prune_artifacts_noop_when_limits_disabled() -> None:
    session = MagicMock()
    store = _FakeStore()

    summary = prune_artifacts(session, store, max_age_days=None, max_bytes_per_org=None)

    assert summary.pruned_records == 0
    assert summary.deleted_files == 0
    assert summary.freed_bytes == 0
    session.execute.assert_not_called()


def test_prune_artifacts_age_eviction_removes_old_artifacts() -> None:
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()

    old_art = Artifact(
        id=uuid.uuid4(),
        org_id=org_id,
        run_id=run_id,
        kind="log",
        content_type="text/plain",
        size_bytes=1024,
        storage_key="org/log/old.log",
        created_at=datetime.now(UTC) - timedelta(days=40),
    )

    store = _FakeStore(existing_keys={"org/log/old.log"})

    session = MagicMock()
    # 1st execute: age query returns [old_art]
    # 2nd execute: count query for remaining refs returns 0
    results = [_FakeResult([old_art]), _FakeResult([], scalar_val=0)]
    session.execute.side_effect = lambda stmt: results.pop(0)

    summary = prune_artifacts(session, store, max_age_days=30, max_bytes_per_org=None)

    assert summary.pruned_records == 1
    assert summary.deleted_files == 1
    assert summary.freed_bytes == 1024
    assert "org/log/old.log" in store.deleted_keys
    session.delete.assert_called_once_with(old_art)
    session.commit.assert_called()


def test_prune_artifacts_dedup_safety_preserves_shared_file() -> None:
    """If another Artifact row shares the content key, the file must not be deleted."""
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()

    shared_key = "org/screenshot/shared.png"
    art_to_delete = Artifact(
        id=uuid.uuid4(),
        org_id=org_id,
        run_id=run_id,
        kind="screenshot",
        content_type="image/png",
        size_bytes=2048,
        storage_key=shared_key,
        created_at=datetime.now(UTC) - timedelta(days=40),
    )

    store = _FakeStore(existing_keys={shared_key})

    session = MagicMock()
    # 1st execute: age query returns [art_to_delete]
    # 2nd execute: count query returns 1 (another record exists with same storage_key)
    results = [_FakeResult([art_to_delete]), _FakeResult([], scalar_val=1)]
    session.execute.side_effect = lambda stmt: results.pop(0)

    summary = prune_artifacts(session, store, max_age_days=30, max_bytes_per_org=None)

    assert summary.pruned_records == 1
    # Physical file was NOT deleted because another record references it
    assert summary.deleted_files == 0
    assert summary.freed_bytes == 2048
    assert len(store.deleted_keys) == 0
    assert shared_key in store.keys
    session.delete.assert_called_once_with(art_to_delete)
