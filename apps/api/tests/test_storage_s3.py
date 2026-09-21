"""The S3 artifact backend, and the property that matters most: it is
interchangeable with the filesystem one.

Driven by a fake S3 client rather than moto or a live bucket. The surface used
is three calls (`put_object`, `get_object`, `head_object`), so a fake that
implements exactly those tests the logic here without pulling a second AWS
emulator into the default install.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

from qagent.config import Settings
from qagent.modules.storage.base import ArtifactNotFound, content_key
from qagent.modules.storage.factory import store_from_settings
from qagent.modules.storage.local import LocalArtifactStore
from qagent.modules.storage.s3 import S3ArtifactStore

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


class _FakeS3:
    """The three calls the store makes, plus counters to assert on."""

    def __init__(self, *, fail_head_with: Exception | None = None) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts = 0
        self.heads = 0
        self._fail_head_with = fail_head_with

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict:  # noqa: N803
        self.puts += 1
        self.objects[(Bucket, Key)] = Body
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise _client_error("NoSuchKey", 404)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        self.heads += 1
        if self._fail_head_with is not None:
            raise self._fail_head_with
        if (Bucket, Key) not in self.objects:
            raise _client_error("404", 404)
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


def _client_error(code: str, status: int) -> Exception:
    """A botocore-shaped error. botocore synthesises its exception classes at
    runtime, so the store matches on the response payload, not the type."""
    exc = Exception(f"{code}")
    exc.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return exc


def _store(client: _FakeS3 | None = None) -> tuple[S3ArtifactStore, _FakeS3]:
    fake = client or _FakeS3()
    return S3ArtifactStore(bucket="evidence", client=fake), fake


# ----------------------------------------------------------------- round trip


def test_save_then_read_returns_the_same_bytes() -> None:
    store, _ = _store()
    data = b"\x89PNG\r\n\x1a\n screenshot bytes"

    stored = store.save(data, org_id=ORG, kind="screenshot", extension=".png")

    assert store.read(stored.storage_key) == data
    assert stored.size_bytes == len(data)


def test_key_layout_matches_the_filesystem_backend(tmp_path: Path) -> None:
    """Both backends must agree, or migrating a root into a bucket stops being
    a recursive copy and becomes a script."""
    data = b"identical bytes"
    s3_store, _ = _store()
    local = LocalArtifactStore(tmp_path)

    s3_key = s3_store.save(data, org_id=ORG, kind="log", extension=".log").storage_key
    local_key = local.save(data, org_id=ORG, kind="log", extension=".log").storage_key

    assert s3_key == local_key
    assert s3_key == content_key(data, org_id=ORG, kind="log", extension=".log")
    assert s3_key.startswith(f"{ORG}/log/")


def test_identical_bytes_are_uploaded_once() -> None:
    store, fake = _store()
    data = b"the same screenshot from two checks"

    first = store.save(data, org_id=ORG, kind="screenshot", extension=".png")
    second = store.save(data, org_id=ORG, kind="screenshot", extension=".png")

    assert first.storage_key == second.storage_key
    assert fake.puts == 1


def test_different_bytes_get_different_keys() -> None:
    store, _ = _store()

    a = store.save(b"one", org_id=ORG, kind="log", extension=".log")
    b = store.save(b"two", org_id=ORG, kind="log", extension=".log")

    assert a.storage_key != b.storage_key


def test_organizations_do_not_share_a_prefix() -> None:
    store, _ = _store()
    other = uuid.uuid4()

    mine = store.save(b"x", org_id=ORG, kind="log", extension=".log")
    theirs = store.save(b"x", org_id=other, kind="log", extension=".log")

    assert mine.storage_key.startswith(f"{ORG}/")
    assert theirs.storage_key.startswith(f"{other}/")


# --------------------------------------------------------------------- errors


def test_missing_key_raises_the_shared_not_found_error() -> None:
    """Callers catch one exception regardless of backend; `main.py` turns it
    into a 404."""
    store, _ = _store()

    with pytest.raises(ArtifactNotFound):
        store.read("11111111-1111-1111-1111-111111111111/log/deadbeef.log")


def test_a_permissions_error_is_not_mistaken_for_absent() -> None:
    """Treating AccessDenied as 'absent' would silently re-upload every
    artifact on every run against a misconfigured bucket."""
    denied = _client_error("AccessDenied", 403)
    store, _ = _store(_FakeS3(fail_head_with=denied))

    with pytest.raises(Exception) as caught:
        store.save(b"x", org_id=ORG, kind="log", extension=".log")

    assert "AccessDenied" in str(caught.value)


def test_a_non_missing_read_error_propagates() -> None:
    class _Broken(_FakeS3):
        def get_object(self, *, Bucket, Key):  # noqa: N803
            raise _client_error("InternalError", 500)

    store, _ = _store(_Broken())

    with pytest.raises(Exception) as caught:
        store.read("k")

    assert not isinstance(caught.value, ArtifactNotFound)


# -------------------------------------------------------------------- factory


def test_default_configuration_selects_the_filesystem_backend(tmp_path: Path) -> None:
    settings = Settings(qagent_artifact_root=str(tmp_path))

    assert isinstance(store_from_settings(settings), LocalArtifactStore)


def test_s3_without_a_bucket_is_refused_not_silently_demoted() -> None:
    """A deployment that believes its evidence is durable, and is really writing
    to a container filesystem, loses exactly what a bug report depends on."""
    settings = Settings(qagent_storage_backend="s3", qagent_s3_bucket=None)

    with pytest.raises(RuntimeError, match="QAGENT_S3_BUCKET"):
        store_from_settings(settings)


def test_s3_backend_is_selected_when_configured(monkeypatch, tmp_path: Path) -> None:
    built: dict = {}

    class _Recorder:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr("qagent.modules.storage.s3.S3ArtifactStore", _Recorder)
    settings = Settings(
        qagent_storage_backend="s3",
        qagent_s3_bucket="evidence",
        qagent_s3_endpoint_url="https://minio.local",
        qagent_s3_region="auto",
    )

    store_from_settings(settings)

    assert built["bucket"] == "evidence"
    assert built["endpoint_url"] == "https://minio.local"
    assert built["region"] == "auto"
