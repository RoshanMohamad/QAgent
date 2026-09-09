"""The QAgent loop: discover, generate, execute, triage, report.

Kept free of database and Celery imports on purpose. The same function backs the
CLI, the worker task and the eval harness, which means the thing measured by the
evaluation suite is exactly the thing that runs in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from qagent.modules.discovery.openapi import EndpointSpec, fetch_spec, parse_openapi
from qagent.modules.generator.rules import GeneratedCase, generate
from qagent.modules.llm.client import LlmClient
from qagent.modules.runner.executor import ApiTestRunner, RunnerConfig, TargetRejected
from qagent.modules.triage.agent import arbitrate, build_bug_report
from qagent.modules.triage.classifier import FailureClass, classify, extract_signals

logger = logging.getLogger(__name__)


@dataclass
class CaseOutcome:
    name: str
    kind: str
    endpoint_key: str | None
    status: str
    duration_ms: int
    request: dict
    response: dict
    assertions: list[dict]
    failure_message: str | None = None
    verdict: dict | None = None
    bug: dict | None = None


@dataclass
class PipelineResult:
    base_url: str
    started_at: datetime
    finished_at: datetime | None = None

    endpoints: list[EndpointSpec] = field(default_factory=list)
    outcomes: list[CaseOutcome] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    llm_totals: dict = field(default_factory=dict)
    spec_url: str | None = None

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")

    @property
    def errored(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "error")

    @property
    def bugs(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.bug]

    def classification_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            if outcome.verdict:
                key = outcome.verdict["failure_class"]
                counts[key] = counts.get(key, 0) + 1
        return counts

    def summary(self) -> dict:
        return {
            "base_url": self.base_url,
            "spec_url": self.spec_url,
            "endpoints": len(self.endpoints),
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "bugs": len(self.bugs),
            "classifications": self.classification_counts(),
            "llm": self.llm_totals,
            "duration_s": round(
                ((self.finished_at or datetime.now(UTC)) - self.started_at).total_seconds(), 2
            ),
        }


def discover(
    base_url: str, openapi_url: str | None = None
) -> tuple[list[EndpointSpec], str | None]:
    document, source_url = fetch_spec(base_url, openapi_url)
    if not document:
        return [], None
    return parse_openapi(document), source_url


def run_pipeline(
    *,
    base_url: str,
    openapi_url: str | None = None,
    auth_headers: dict[str, str] | None = None,
    default_headers: dict[str, str] | None = None,
    max_cases: int | None = None,
    timeout_seconds: float = 30.0,
    allow_private: bool = True,
    allowlist: list[str] | None = None,
    llm: LlmClient | None = None,
    history: dict[str, list[str]] | None = None,
) -> PipelineResult:
    """Run the full loop against one environment.

    ``history`` maps a case name to its recent pass/fail sequence and is what lets the
    classifier recognise flakiness. The CLI passes nothing; the worker passes what it
    reads from test_results.
    """
    llm = llm or LlmClient.from_settings()
    result = PipelineResult(base_url=base_url, started_at=datetime.now(UTC))

    # --- discover -----------------------------------------------------------
    endpoints, spec_url = discover(base_url, openapi_url)
    result.endpoints = endpoints
    result.spec_url = spec_url

    if not endpoints:
        result.errors.append(
            "No OpenAPI document could be retrieved. Supply one explicitly, or the "
            "project needs route parsing, which is not yet implemented."
        )
        result.finished_at = datetime.now(UTC)
        result.llm_totals = llm.totals()
        return result

    # --- generate -----------------------------------------------------------
    generation = generate(endpoints, max_cases=max_cases)
    result.skipped.extend(generation.skipped)
    logger.info("generated %d cases across %d endpoints", len(generation.cases), len(endpoints))

    # --- execute ------------------------------------------------------------
    config = RunnerConfig(
        base_url=base_url,
        default_headers=default_headers or {},
        auth_headers=auth_headers or {},
        timeout_seconds=timeout_seconds,
        allow_private=allow_private,
        allowlist=allowlist or [],
    )

    try:
        runner = ApiTestRunner(config)
    except TargetRejected as exc:
        result.errors.append(f"target rejected by egress policy: {exc}")
        result.finished_at = datetime.now(UTC)
        result.llm_totals = llm.totals()
        return result

    auth_configured = bool(auth_headers)

    with runner:
        for case in generation.cases:
            outcome = _execute_and_triage(
                case=case,
                runner=runner,
                llm=llm,
                auth_configured=auth_configured,
                history=(history or {}).get(case.name, []),
            )
            result.outcomes.append(outcome)

    result.finished_at = datetime.now(UTC)
    result.llm_totals = llm.totals()
    return result


def _execute_and_triage(
    *,
    case: GeneratedCase,
    runner: ApiTestRunner,
    llm: LlmClient,
    auth_configured: bool,
    history: list[str],
) -> CaseOutcome:
    execution = runner.execute(case.spec)

    outcome = CaseOutcome(
        name=case.name,
        kind=case.kind,
        endpoint_key=case.endpoint_key,
        status=execution.status,
        duration_ms=execution.duration_ms,
        request=execution.request,
        response=execution.response,
        assertions=execution.assertions,
        failure_message=execution.failure_message,
    )

    if execution.passed:
        return outcome

    signals = extract_signals(
        spec=case.spec,
        request=execution.request,
        response=execution.response,
        failure_message=execution.failure_message,
        auth_configured=auth_configured,
        recent_history=history,
    )
    verdict = classify(signals)
    verdict = arbitrate(
        verdict,
        signals,
        spec=case.spec,
        request=execution.request,
        response=execution.response,
        failure_message=execution.failure_message,
        llm=llm,
    )
    outcome.verdict = verdict.to_dict()

    # Only real defects become bug reports. Everything else stays a classified
    # failure, which is the whole point of the triage stage.
    if verdict.failure_class is FailureClass.REAL_BUG:
        spec_with_kind = {**case.spec, "kind": case.kind}
        outcome.bug = build_bug_report(
            case_name=case.name,
            verdict=verdict,
            spec=spec_with_kind,
            request=execution.request,
            response=execution.response,
            failure_message=execution.failure_message,
            llm=llm,
        )

    return outcome
