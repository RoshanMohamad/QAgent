"""The QAgent loop: discover, generate, execute, triage, report.

Kept free of database and Celery imports on purpose. The same function backs the
CLI, the worker task and the eval harness, which means the thing measured by the
evaluation suite is exactly the thing that runs in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from qagent.modules.discovery.openapi import EndpointSpec, fetch_spec, parse_openapi
from qagent.modules.explorer.actions import InteractionPolicy
from qagent.modules.generator.rules import GeneratedCase, generate
from qagent.modules.llm.client import LlmClient
from qagent.modules.observability.tracing import set_attributes, span
from qagent.modules.planner.strategy import CoverageReport, TestPlan, build_plan, coverage
from qagent.modules.rag.index import RepositoryIndex
from qagent.modules.runner.executor import ApiTestRunner, RunnerConfig, TargetRejected
from qagent.modules.triage.agent import arbitrate, build_bug_report
from qagent.modules.triage.classifier import (
    FailureClass,
    Verdict,
    classify,
    extract_signals,
)

if TYPE_CHECKING:
    from qagent.modules.browser.runner import PageCheckResult
    from qagent.modules.explorer.actions import ActionOutcome

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArtifactBytes:
    """Evidence captured in-memory during a check. Nothing in this module ever
    touches storage or the database - `persistence.py` is the only place that
    writes these to disk and creates the `Artifact` row (CLAUDE.md section 15).
    """

    kind: str  # "screenshot" | "log"
    content_type: str
    extension: str
    data: bytes


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
    artifacts: list[ArtifactBytes] = field(default_factory=list)


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
    plan: TestPlan | None = None
    coverage: CoverageReport | None = None

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

    def surface_coverage(self) -> dict:
        """How much of the discovered surface a check actually ran against.

        Distinct from ``coverage``, which is plan-vs-actual: a plan can be 100%
        covered while half the API is untouched, because the plan itself was
        capped. Both numbers are true and they answer different questions.
        """
        from qagent.modules.coverage.surface import summarise

        return summarise(self).to_dict()

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
            "plan": self.plan.summary() if self.plan else None,
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "surface": self.surface_coverage(),
            "llm": self.llm_totals,
            "duration_s": round(
                ((self.finished_at or datetime.now(UTC)) - self.started_at).total_seconds(), 2
            ),
        }


def discover(
    base_url: str, openapi_url: str | None = None, repo_path: Path | None = None
) -> tuple[list[EndpointSpec], str | None]:
    """Discover endpoints from an OpenAPI document, falling back to static route
    parsing of ``repo_path`` when the project doesn't expose one.
    """
    document, source_url = fetch_spec(base_url, openapi_url)
    if document:
        return parse_openapi(document), source_url

    if repo_path is not None:
        from qagent.modules.discovery.routes import parse_routes

        endpoints = parse_routes(repo_path)
        if endpoints:
            return endpoints, f"route-parser:{repo_path}"

    return [], None


def run_pipeline(
    *,
    base_url: str,
    openapi_url: str | None = None,
    repo_path: Path | None = None,
    auth_headers: dict[str, str] | None = None,
    default_headers: dict[str, str] | None = None,
    max_cases: int | None = None,
    timeout_seconds: float = 30.0,
    allow_private: bool = True,
    allowlist: list[str] | None = None,
    llm: LlmClient | None = None,
    history: dict[str, list[str]] | None = None,
    run_e2e: bool = False,
    e2e_max_pages: int = 15,
    run_interactive: bool = False,
    interactive_max_pages: int = 15,
    interactive_max_actions: int = 40,
    interaction_policy: InteractionPolicy | None = None,
    plan_enrichment: bool = False,
    code_index: RepositoryIndex | None = None,
    secondary_auth_headers: dict[str, str] | None = None,
) -> PipelineResult:
    """Run the full loop against one environment.

    ``repo_path``, when given, is a static fallback: it's only consulted when no
    OpenAPI document can be fetched, to statically parse routes out of the source
    instead (see ``modules/discovery/routes.py``).

    ``history`` maps a case name to its recent pass/fail sequence and is what lets the
    classifier recognise flakiness. The CLI passes nothing; the worker passes what it
    reads from test_results.

    ``run_e2e`` crawls same-origin pages from ``base_url`` (modules/explorer/crawler)
    and page-checks every one of them (modules/browser/runner), folding the result
    into ``outcomes`` as ``kind="e2e"`` so E2E findings share one persistence path,
    one dashboard and one quality gate with API results. It is optional and degrades
    to a recorded, non-fatal skip when Playwright isn't installed, since it's the
    only stage with a dependency the core install doesn't carry.

    ``plan_enrichment`` lets the Test Planner (agent 2) spend a model call raising
    the priority of modules whose business impact the rules under-rated. Off by
    default: the plan itself is always built, always rules-derived, and always
    free - enrichment can only reorder what is already there.

    ``code_index`` is a repository index (modules/rag/). When supplied, a failure
    classified as a real defect is used as a retrieval query against the checkout,
    and the matching functions are attached to the bug report as ``affected_code``
    - the "Affected: OrderService.createOrder()" line CLAUDE.md section 13 asks
    for. Retrieval runs with or without a model configured.

    ``secondary_auth_headers`` is a *second* identity's credentials. Supplying
    them enables the IDOR probe (modules/security/idor.py), which is the one
    check that cannot be a generator rule: broken object-level authorization is
    by definition the difference between what two identities can reach, and
    every rule tests one at a time. Without a second identity the stage is
    skipped and recorded as such.

    ``run_interactive`` additionally fills forms and clicks through same-origin pages
    (modules/explorer/interact.py) instead of only following links, folding any defect
    it finds into ``outcomes`` as ``kind="e2e_interactive"``. It is a separate stage
    from ``run_e2e`` (own caps, own failure mode) so either can be enabled without the
    other, and degrades the same way: a non-fatal skip when Playwright isn't installed.
    """
    llm = llm or LlmClient.from_settings()
    result = PipelineResult(base_url=base_url, started_at=datetime.now(UTC))

    # --- discover -----------------------------------------------------------
    with span("qagent.discover", base_url=base_url) as active:
        endpoints, spec_url = discover(base_url, openapi_url, repo_path)
        set_attributes(active, endpoints=len(endpoints), spec_url=spec_url or "none")
    result.endpoints = endpoints
    result.spec_url = spec_url

    if not endpoints:
        result.errors.append(
            "No OpenAPI document could be retrieved and route parsing found nothing "
            "usable." + ("" if repo_path else " Supply --repo to try static route parsing.")
        )
        result.finished_at = datetime.now(UTC)
        result.llm_totals = llm.totals()
        return result

    # --- plan ---------------------------------------------------------------
    # Rules-first and free; `enrich_plan` is the opt-in model pass on top, and is
    # deliberately not called here so the default loop stays $0.00 (README).
    with span("qagent.plan") as active:
        plan = build_plan(endpoints)
        if plan_enrichment and llm.available:
            from qagent.modules.planner.strategy import enrich_plan

            plan = enrich_plan(plan, llm)
        set_attributes(
            active, modules=len(plan.modules), required_checks=plan.required_checks
        )
    result.plan = plan

    # --- generate -----------------------------------------------------------
    with span("qagent.generate") as active:
        generation = generate(endpoints, max_cases=max_cases, plan=plan)
        result.skipped.extend(generation.skipped)
        result.coverage = coverage(plan, generation)
        set_attributes(
            active,
            cases=len(generation.cases),
            coverage_ratio=result.coverage.ratio,
            uncovered_modules=len(result.coverage.uncovered_modules),
        )
    logger.info(
        "generated %d cases across %d endpoints (%d/%d planned checks covered)",
        len(generation.cases),
        len(endpoints),
        result.coverage.generated,
        result.coverage.planned,
    )

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

    # Before the generated cases run, not after. The probe is read-only and
    # needs the application's data as it found it - and generation deliberately
    # produces destructive cases (`DELETE /tasks/{id}`) that had already removed
    # the row the probe was about to use as evidence. A read-only check that
    # runs downstream of a destructive one is measuring the wrong application.
    if secondary_auth_headers:
        _run_idor_stage(
            result,
            base_url=base_url,
            endpoints=endpoints,
            primary=auth_headers or {},
            secondary=secondary_auth_headers,
            timeout_seconds=timeout_seconds,
            llm=llm,
        )

    with span("qagent.execute", cases=len(generation.cases)) as active, runner:
        for case in generation.cases:
            outcome = _execute_and_triage(
                case=case,
                runner=runner,
                llm=llm,
                auth_configured=auth_configured,
                history=(history or {}).get(case.name, []),
                code_index=code_index,
            )
            result.outcomes.append(outcome)
        set_attributes(
            active, passed=result.passed, failed=result.failed, errored=result.errored
        )

    if run_e2e:
        _run_e2e_stage(
            result,
            base_url=base_url,
            max_pages=e2e_max_pages,
            timeout_seconds=timeout_seconds,
            llm=llm,
        )

    if run_interactive:
        _run_interactive_stage(
            result,
            base_url=base_url,
            max_pages=interactive_max_pages,
            max_total_actions=interactive_max_actions,
            policy=interaction_policy,
            timeout_seconds=timeout_seconds,
            llm=llm,
        )

    result.finished_at = datetime.now(UTC)
    result.llm_totals = llm.totals()
    return result


def _run_idor_stage(
    result: PipelineResult,
    *,
    base_url: str,
    endpoints: list[EndpointSpec],
    primary: dict[str, str],
    secondary: dict[str, str],
    timeout_seconds: float,
    llm: LlmClient,
) -> None:
    """Probe for broken object-level authorization with two identities.

    Folded into ``outcomes`` as ``kind="api_security"`` so an IDOR shares one
    persistence path, one dashboard and one quality gate with everything else -
    the same reason the browser stages fold their findings in rather than
    reporting separately.

    Isolated in its own function, like the browser stages, so a failure here
    never loses the API results already collected above it.
    """
    import httpx

    from qagent.modules.security.idor import probe_endpoints

    try:
        identities = {"primary": primary, "secondary": secondary}
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds) as client:

            def request(method: str, path: str, identity: str):
                try:
                    response = client.request(method, path, headers=identities[identity])
                except httpx.HTTPError:
                    return None, None, None
                try:
                    parsed = response.json()
                except ValueError:
                    parsed = None
                return response.status_code, response.text, parsed

            probe = probe_endpoints(endpoints, request=request)
    except Exception as exc:  # noqa: BLE001 - never sink results already collected
        logger.exception("IDOR probe failed")
        result.errors.append(f"IDOR probe failed: {exc}")
        return

    for finding in probe.findings:
        outcome = CaseOutcome(
            name=f"{finding.path} enforces object-level authorization",
            kind="api_security",
            endpoint_key=finding.path,
            status="failed",
            duration_ms=0,
            request={"method": "GET", "path": finding.path, "auth": "secondary"},
            response={"status": 200},
            assertions=[],
            failure_message=finding.message,
        )
        # Rules-derived and certain: the probe only reports when two identities
        # received the same body, so there is nothing for the classifier to be
        # unsure about and no reason to spend a model call arbitrating it.
        verdict = Verdict(
            failure_class=FailureClass.REAL_BUG,
            confidence=0.95,
            reason=finding.message,
        )
        outcome.verdict = verdict.to_dict()
        outcome.bug = build_bug_report(
            case_name=outcome.name,
            verdict=verdict,
            spec={"expectation": "A resource is readable only by the identity that owns it.",
                  "kind": "api_security"},
            request=outcome.request,
            response=outcome.response,
            failure_message=finding.message,
            llm=llm,
        )
        result.outcomes.append(outcome)

    for endpoint_key, reason in probe.inconclusive.items():
        # Recorded, not silent: "no IDOR found" and "could not check" are
        # different claims and a security result must not conflate them.
        result.skipped.append(f"IDOR probe inconclusive for {endpoint_key}: {reason}")


def _run_e2e_stage(
    result: PipelineResult, *, base_url: str, max_pages: int, timeout_seconds: float, llm: LlmClient
) -> None:
    """Crawl, page-check, and fold the findings into ``result.outcomes``.

    Isolated in its own function so a browser-stage failure (missing Playwright,
    a crash mid-crawl) never loses the API results already collected above it.
    """
    try:
        from qagent.modules.browser.runner import run_browser_checks
        from qagent.modules.explorer.crawler import explore

        # Playwright is imported lazily inside explore()/run_browser_checks(), not
        # at the module level, so the ModuleNotFoundError surfaces from these calls
        # rather than from the imports above.
        graph = explore(base_url=base_url, max_pages=max_pages, timeout_seconds=timeout_seconds)
        browser_result = run_browser_checks(
            base_url=base_url, routes=graph.routes, timeout_seconds=timeout_seconds
        )
    except ModuleNotFoundError:
        result.errors.append(
            "E2E stage skipped: playwright is not installed (pip install qagent[e2e] "
            "&& playwright install chromium)."
        )
        return
    except Exception as exc:  # noqa: BLE001 - never sink API results already collected
        logger.exception("e2e exploration failed")
        result.errors.append(f"E2E stage failed: {exc}")
        return

    for check in browser_result.checks:
        result.outcomes.append(_page_check_to_outcome(check, llm))


def _run_interactive_stage(
    result: PipelineResult,
    *,
    base_url: str,
    max_pages: int,
    max_total_actions: int,
    policy: InteractionPolicy | None,
    timeout_seconds: float,
    llm: LlmClient,
) -> None:
    """Fill forms, click through the app, and fold any defect found into
    ``result.outcomes``. Isolated the same way ``_run_e2e_stage`` is, so a
    browser-stage failure never loses the API (or plain-E2E) results already
    collected above it.
    """
    try:
        from qagent.modules.explorer.interact import explore_interactive

        graph = explore_interactive(
            base_url=base_url,
            max_pages=max_pages,
            max_total_actions=max_total_actions,
            policy=policy,
            timeout_seconds=timeout_seconds,
            llm=llm,
        )
    except ModuleNotFoundError:
        result.errors.append(
            "Interactive exploration skipped: playwright is not installed (pip install "
            "qagent[e2e] && playwright install chromium)."
        )
        return
    except Exception as exc:  # noqa: BLE001 - never sink results already collected
        logger.exception("interactive exploration failed")
        result.errors.append(f"Interactive exploration failed: {exc}")
        return

    for node in graph.nodes.values():
        for outcome in node.actions_taken:
            result.outcomes.append(_action_outcome_to_case_outcome(outcome, llm))


def _action_outcome_to_case_outcome(outcome: ActionOutcome, llm: LlmClient) -> CaseOutcome:
    from qagent.modules.explorer.actions import classify_action_outcome

    action = outcome.action
    status = "passed" if outcome.ok and not (outcome.page_errors or outcome.console_errors) else (
        "failed" if outcome.ok else "error"
    )

    element_desc = action.target.text or action.target.selector
    case_outcome = CaseOutcome(
        name=f"{action.type.value} {action.target.tag} ({element_desc})",
        kind="e2e_interactive",
        endpoint_key=None,
        status=status,
        duration_ms=0,
        request={
            "method": action.type.value,
            "path": action.target.selector,
            "value": action.value,
        },
        response={
            "status": None,
            "url": outcome.resulting_url,
            "body_text": "\n".join(outcome.console_errors + outcome.page_errors) or None,
        },
        assertions=[],
        failure_message=outcome.error,
    )

    verdict = classify_action_outcome(outcome)
    if verdict is None:
        return case_outcome

    case_outcome.verdict = verdict.to_dict()
    if verdict.failure_class is FailureClass.REAL_BUG:
        case_outcome.bug = build_bug_report(
            case_name=case_outcome.name,
            verdict=verdict,
            spec={
                "expectation": "The action completes without triggering a server error or "
                "an uncaught exception.",
                "kind": "e2e_interactive",
            },
            request=case_outcome.request,
            response=case_outcome.response,
            failure_message=outcome.error,
            llm=llm,
        )
    return case_outcome


def _page_check_to_outcome(check: PageCheckResult, llm: LlmClient) -> CaseOutcome:
    from qagent.modules.browser.triage import classify_page_check

    outcome = CaseOutcome(
        name=f"page loads: {check.url}",
        kind="e2e",
        endpoint_key=None,
        status=check.status,
        duration_ms=check.load_time_ms,
        request={"method": "GET", "path": check.url},
        response={
            "status": check.http_status,
            "duration_ms": check.load_time_ms,
            "body_text": "\n".join(check.console_errors + check.page_errors) or None,
        },
        assertions=[],
        failure_message=check.failure_message,
    )

    verdict = classify_page_check(check)
    if verdict is None:
        return outcome

    outcome.verdict = verdict.to_dict()
    if verdict.failure_class is FailureClass.REAL_BUG:
        outcome.bug = build_bug_report(
            case_name=outcome.name,
            verdict=verdict,
            spec={
                "expectation": "The page loads without a server error or an uncaught exception.",
                "kind": "e2e",
            },
            request=outcome.request,
            response=outcome.response,
            failure_message=check.failure_message,
            llm=llm,
        )
        # Evidence, only for what actually became a bug report - a screenshot on
        # every passing page load would be pure storage cost for no reader.
        if check.screenshot_png:
            outcome.artifacts.append(
                ArtifactBytes(
                    kind="screenshot",
                    content_type="image/png",
                    extension=".png",
                    data=check.screenshot_png,
                )
            )
        console_log = "\n".join(check.console_errors + check.page_errors)
        if console_log:
            outcome.artifacts.append(
                ArtifactBytes(
                    kind="log",
                    content_type="text/plain",
                    extension=".log",
                    data=console_log.encode("utf-8"),
                )
            )
    return outcome


def _execute_and_triage(
    *,
    case: GeneratedCase,
    runner: ApiTestRunner,
    llm: LlmClient,
    auth_configured: bool,
    history: list[str],
    code_index: RepositoryIndex | None = None,
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
            index=code_index,
        )

    return outcome
