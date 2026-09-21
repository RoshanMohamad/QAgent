"""A small client for QAgent's own API, used by the CLI.

`qagent gate` is deliberately database-free: a pull request has no project row,
no org and no history, and requiring one would mean standing up Postgres to
find out whether a branch is safe to merge. That property is worth keeping.

But a gate decision that is only ever computed cannot be audited. Nobody can
answer "what did the gate say when we shipped the release that broke
production", because the numbers it saw have since changed. So the decision is
*optionally* reported to a QAgent deployment, and this is the only thing in the
CLI that talks to one.

Optional is the operative word. Every method here returns whether it succeeded
and never raises: a QAgent API that is down must not fail a CI job whose gate
already passed. Reporting is bookkeeping; the exit code is the decision.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0


@dataclass
class ReportResult:
    ok: bool
    detail: str | None = None
    record_id: str | None = None


@dataclass
class QAgentClient:
    base_url: str
    token: str | None = None

    @classmethod
    def from_env(cls, base_url: str | None = None, token: str | None = None) -> QAgentClient | None:
        """Build a client from flags or environment, or None when unconfigured.

        None rather than an exception: not reporting is the normal case, and a
        CLI that demanded an API URL would break every offline invocation.
        """
        url = base_url or os.environ.get("QAGENT_API_URL")
        if not url:
            return None
        return cls(base_url=url.rstrip("/"), token=token or os.environ.get("QAGENT_TOKEN"))

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    def _post(self, path: str, payload: dict) -> ReportResult:
        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                response = client.post(
                    f"{self.base_url}{path}", json=payload, headers=self._headers()
                )
            if 200 <= response.status_code < 300:
                body = response.json() if response.content else {}
                return ReportResult(ok=True, record_id=body.get("id"))
            return ReportResult(
                ok=False, detail=f"HTTP {response.status_code}: {response.text[:200]}"
            )
        except Exception as exc:  # noqa: BLE001 - reporting never fails a gate
            logger.warning("could not reach the QAgent API: %s", exc)
            return ReportResult(ok=False, detail=str(exc)[:200])

    def record_gate(self, project_id: str, *, decision: dict, run_id: str | None = None,
                    commit_sha: str | None = None, trigger: str = "ci") -> ReportResult:
        """Store a gate verdict with the inputs it was made from."""
        return self._post(
            f"/api/v1/projects/{project_id}/gates",
            {
                "run_id": run_id,
                "result": decision["result"],
                "reason": "; ".join(decision.get("reasons") or []) or None,
                "commit_sha": commit_sha,
                "trigger": trigger,
                "policy": decision.get("policy") or {},
                "checks": decision.get("checks") or [],
            },
        )

    def record_deployment(self, project_id: str, *, status: str, commit_sha: str | None = None,
                          version: str | None = None,
                          quality_gate_id: str | None = None) -> ReportResult:
        return self._post(
            f"/api/v1/projects/{project_id}/deployments",
            {
                "status": status,
                "commit_sha": commit_sha,
                "version": version,
                "quality_gate_id": quality_gate_id,
            },
        )
