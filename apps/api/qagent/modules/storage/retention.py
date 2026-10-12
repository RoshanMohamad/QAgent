"""Artifact retention and eviction policy (CLAUDE.md section 15, README roadmap).

Evidence artifacts (screenshots, logs, HARs, and Playwright traces) provide vital
proof for defects, but unconstrained retention will consume unbounded disk or
object-storage capacity over time.

This module implements deterministic retention and quota pruning:
1. Max age eviction: Drops artifacts older than `max_age_days`.
2. Org quota eviction: When an organization's cumulative artifact size exceeds
   `max_bytes_per_org`, evicts the oldest artifacts until under quota.
3. Content-address safety: Because storage is content-addressed, a physical file
   is only deleted from storage if no remaining `Artifact` record points to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qagent import models
from qagent.modules.storage.base import ArtifactStore

logger = logging.getLogger(__name__)


@dataclass
class PruneSummary:
    pruned_records: int = 0
    freed_bytes: int = 0
    deleted_files: int = 0
    errors: list[str] = field(default_factory=list)


def prune_artifacts(
    session: Session,
    store: ArtifactStore,
    *,
    max_age_days: int | None = None,
    max_bytes_per_org: int | None = None,
    org_id: UUID | None = None,
) -> PruneSummary:
    """Prune expired and quota-exceeding artifacts.

    Args:
        session: Active SQLAlchemy database session.
        store: ArtifactStore backend implementing ``delete()``.
        max_age_days: Optional age cutoff in days.
        max_bytes_per_org: Optional maximum bytes permitted per organization.
        org_id: Optional organization filter to restrict pruning to one tenant.
    """
    summary = PruneSummary()
    candidates_to_delete: list[models.Artifact] = []

    # 1. Age-based expiration
    if max_age_days is not None and max_age_days > 0:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        age_stmt = select(models.Artifact).where(models.Artifact.created_at < cutoff)
        if org_id is not None:
            age_stmt = age_stmt.where(models.Artifact.org_id == org_id)
        candidates_to_delete.extend(session.execute(age_stmt).scalars().all())

    candidate_ids = {a.id for a in candidates_to_delete}

    # 2. Quota-based eviction per organization
    if max_bytes_per_org is not None and max_bytes_per_org > 0:
        # Determine orgs to inspect
        if org_id is not None:
            target_orgs = [org_id]
        else:
            target_orgs = list(
                session.execute(select(models.Artifact.org_id).distinct()).scalars().all()
            )

        for current_org in target_orgs:
            # Current total size excluding already scheduled candidates
            all_org_artifacts = (
                session.execute(
                    select(models.Artifact)
                    .where(models.Artifact.org_id == current_org)
                    .order_by(models.Artifact.created_at.asc())
                )
                .scalars()
                .all()
            )

            remaining = [a for a in all_org_artifacts if a.id not in candidate_ids]
            current_total = sum(a.size_bytes for a in remaining)

            # Evict oldest first until under quota
            for art in remaining:
                if current_total <= max_bytes_per_org:
                    break
                candidates_to_delete.append(art)
                candidate_ids.add(art.id)
                current_total -= art.size_bytes

    if not candidates_to_delete:
        return summary

    # Group storage keys and calculate freed sizes
    storage_keys_to_check: set[str] = set()
    for art in candidates_to_delete:
        summary.pruned_records += 1
        summary.freed_bytes += art.size_bytes
        storage_keys_to_check.add(art.storage_key)
        session.delete(art)

    session.flush()

    # Safely delete physical files from storage:
    # only delete if no other Artifact records in the DB still reference the key.
    for key in storage_keys_to_check:
        still_referenced = session.execute(
            select(func.count(models.Artifact.id)).where(models.Artifact.storage_key == key)
        ).scalar_one()

        if still_referenced == 0:
            try:
                deleted = store.delete(key)
                if deleted:
                    summary.deleted_files += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to delete artifact file %s: %s", key, exc)
                summary.errors.append(f"{key}: {exc}")

    session.commit()
    logger.info(
        "pruned %d artifact records, freed %d bytes, deleted %d files",
        summary.pruned_records,
        summary.freed_bytes,
        summary.deleted_files,
    )
    return summary
