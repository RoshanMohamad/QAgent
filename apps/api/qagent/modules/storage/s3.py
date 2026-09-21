"""S3-compatible object storage for evidence artifacts.

The backend a real deployment uses, and the one the README's stack table has
named since before anything implemented it: S3 itself, Cloudflare R2, MinIO,
or any other service speaking the same API. `endpoint_url` is what makes those
interchangeable - unset for AWS, set for everything else.

boto3 is an optional dependency (``pip install qagent[s3]``). Importing it
lazily, inside ``__init__`` rather than at module scope, is deliberate: the
default install has no AWS SDK, and a module-level import would make
``qagent.modules.storage`` unimportable for everyone who never asked for S3.

Nothing here retries by hand. botocore already has a configurable retry policy
that understands which S3 errors are transient, and a hand-rolled loop on top
of it retries the ones it knows are hopeless.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from qagent.modules.storage.base import ArtifactNotFound, StoredArtifact, content_key

logger = logging.getLogger(__name__)

#: Errors S3 returns when an object or bucket is absent. Checked by code rather
#: than by exception type because botocore synthesises its exception classes at
#: runtime, so there is no stable class to catch.
_MISSING_CODES = {"NoSuchKey", "404", "NoSuchBucket", "NotFound"}


class S3ArtifactStore:
    """Objects in one bucket, keyed ``<org>/<kind>/<sha256><ext>``.

    The same layout the filesystem backend uses, so migrating between them is a
    recursive copy rather than a script.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        client: Any = None,
    ) -> None:
        self.bucket = bucket
        if client is not None:
            # Injected for tests, and for any deployment that needs to build a
            # session in a way this constructor does not cover (SSO, assumed
            # roles, IMDS credentials).
            self._client = client
            return

        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - configuration error path
            raise RuntimeError(
                "QAGENT_STORAGE_BACKEND=s3 needs boto3: pip install 'qagent[s3]'"
            ) from exc

        # Credentials left as None fall through to boto3's own chain -
        # environment, shared config, instance role - which is what any
        # deployment running on AWS should actually use. Passing keys
        # explicitly is for MinIO and for local development.
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    def save(
        self, data: bytes, *, org_id: UUID | str, kind: str, extension: str
    ) -> StoredArtifact:
        key = content_key(data, org_id=org_id, kind=kind, extension=extension)

        # Content-addressed, so an object already at this key holds exactly
        # these bytes. Skipping the upload saves the transfer, and a HEAD is
        # far cheaper than a PUT of a multi-megabyte screenshot.
        if not self._exists(key):
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data)

        return StoredArtifact(storage_key=key, size_bytes=len(data))

    def read(self, storage_key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=storage_key)
        except Exception as exc:  # noqa: BLE001 - botocore's classes are synthesised
            if _is_missing(exc):
                raise ArtifactNotFound(storage_key) from exc
            raise
        return response["Body"].read()

    def _exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - botocore's classes are synthesised
            if _is_missing(exc):
                return False
            # A permissions or network error is not "absent". Treating it as
            # absent would turn a misconfigured bucket into a silent re-upload
            # of every artifact on every run.
            raise
        return True


def _is_missing(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    code = str(error.get("Code", ""))
    status = str((response.get("ResponseMetadata") or {}).get("HTTPStatusCode", ""))
    return code in _MISSING_CODES or status == "404"
