"""Pick the artifact backend from configuration.

One place that knows both backends exist, so neither has to import the other
and no call site has to branch. `persistence.py` and the artifact endpoint call
`store_from_settings()` and get whatever the deployment configured.
"""

from __future__ import annotations

import logging

from qagent.modules.storage.base import ArtifactStore

logger = logging.getLogger(__name__)


def store_from_settings(settings=None) -> ArtifactStore:
    if settings is None:
        from qagent.config import get_settings

        settings = get_settings()

    backend = getattr(settings, "qagent_storage_backend", "local")

    if backend == "s3":
        from qagent.modules.storage.s3 import S3ArtifactStore

        if not settings.qagent_s3_bucket:
            # Refused rather than silently demoted to local storage: a
            # deployment that thinks its evidence is in a durable bucket, and
            # is really writing to a container filesystem that vanishes on the
            # next deploy, loses exactly the evidence a bug report depends on.
            raise RuntimeError(
                "QAGENT_STORAGE_BACKEND=s3 requires QAGENT_S3_BUCKET to be set"
            )

        logger.info(
            "artifact storage: s3 bucket=%s endpoint=%s",
            settings.qagent_s3_bucket,
            settings.qagent_s3_endpoint_url or "aws",
        )
        return S3ArtifactStore(
            bucket=settings.qagent_s3_bucket,
            endpoint_url=settings.qagent_s3_endpoint_url,
            region=settings.qagent_s3_region,
            access_key=settings.qagent_s3_access_key,
            secret_key=settings.qagent_s3_secret_key,
        )

    from qagent.modules.storage.local import LocalArtifactStore

    return LocalArtifactStore(settings.qagent_artifact_root)
