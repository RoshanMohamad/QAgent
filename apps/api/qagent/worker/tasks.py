"""Celery tasks.

A scan runs against a third-party application over the network and can take minutes,
so it never runs inside a request. Celery over Redis is deliberate: reference image 05
shows an Argo Workflows topology, which is the right answer only once the platform is
on Kubernetes and needs distributed scheduling. Until then it is machinery without a
payload. See ADR-0001 on resisting premature architecture.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from celery import Celery

from qagent import models
from qagent.config import get_settings
from qagent.db import session_scope
from qagent.modules.llm.client import LlmClient
from qagent.persistence import persist_agent_run, persist_result, recent_history
from qagent.pipeline import run_pipeline

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery(
    "qagent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=1800,
    task_soft_time_limit=1500,
    task_default_queue="qagent",
)


@celery_app.task(bind=True, name="qagent.run_scan", max_retries=2)
def run_scan(self, org_id: str, project_id: str, run_id: str) -> dict:
    """Execute one scan and persist everything it produced."""
    org = UUID(org_id)
    project = UUID(project_id)

    with session_scope(org) as session:
        run = session.get(models.TestRun, UUID(run_id))
        if run is None:
            return {"error": "run not found"}

        environment = session.get(models.Environment, run.environment_id) if run.environment_id else None
        if environment is None or not environment.base_url:
            run.status = models.RunStatus.ERROR
            return {"error": "environment has no base_url"}

        run.status = models.RunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        base_url = environment.base_url
        openapi_url = environment.openapi_url
        headers = dict(environment.default_headers or {})
        history = recent_history(session, org, project)

    llm = LlmClient.from_settings(settings)

    try:
        result = run_pipeline(
            base_url=base_url,
            openapi_url=openapi_url,
            default_headers=headers,
            auth_headers=_resolve_secret(environment_secret_ref=None),
            timeout_seconds=settings.qagent_runner_timeout_seconds,
            allow_private=not settings.is_production,
            allowlist=settings.egress_allowlist,
            llm=llm,
            history=history,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("scan failed")
        with session_scope(org) as session:
            run = session.get(models.TestRun, UUID(run_id))
            if run:
                run.status = models.RunStatus.ERROR
        raise self.retry(exc=exc, countdown=30) from exc

    with session_scope(org) as session:
        run = session.get(models.TestRun, UUID(run_id))
        persist_result(session, org_id=org, project_id=project, run=run, result=result)
        persist_agent_run(
            session,
            org_id=org,
            project_id=project,
            run_id=run.id,
            agent="api_qa_pipeline",
            llm_totals=result.llm_totals,
            records=llm.records,
        )

    return result.summary()


def _resolve_secret(*, environment_secret_ref: str | None) -> dict[str, str]:
    """Fetch per-environment credentials at execution time.

    Deliberately a seam rather than an implementation: credentials must come from a
    secret manager and be injected for the duration of one run, never stored on the
    environment row or written into artifacts (ADR-0004).
    """
    if not environment_secret_ref:
        return {}
    raise NotImplementedError("secret manager integration is not wired up yet")
