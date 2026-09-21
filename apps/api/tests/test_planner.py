"""Agent 2 - the Test Planner: priority derivation, budget ordering, and the
coverage report that holds generation to what the plan promised.
"""

from __future__ import annotations

from qagent.modules.discovery.openapi import EndpointSpec, score_risk
from qagent.modules.generator.rules import applicable_rules, generate
from qagent.modules.llm.client import LlmClient
from qagent.modules.planner.strategy import (
    Priority,
    build_plan,
    coverage,
    enrich_plan,
)


def _endpoint(method: str, path: str, *, requires_auth: bool = False, **kw) -> EndpointSpec:
    return EndpointSpec(
        method=method,
        path=path,
        requires_auth=requires_auth,
        risk_score=score_risk(method, path, requires_auth),
        **kw,
    )


# --------------------------------------------------------------------- plan shape


def test_empty_endpoints_plans_nothing_and_warns() -> None:
    plan = build_plan([])

    assert plan.modules == []
    assert plan.required_checks == 0
    assert any("no endpoints" in w for w in plan.warnings)


def test_auth_module_outranks_a_plain_read_module() -> None:
    plan = build_plan(
        [
            _endpoint("POST", "/api/v1/auth/login"),
            _endpoint("GET", "/api/v1/health"),
        ]
    )

    names = [m.name for m in plan.modules]
    assert names.index("auth") < names.index("health")

    auth = next(m for m in plan.modules if m.name == "auth")
    health = next(m for m in plan.modules if m.name == "health")
    assert auth.priority is Priority.CRITICAL
    assert health.priority is Priority.LOW


def test_every_priority_carries_a_reason() -> None:
    plan = build_plan(
        [
            _endpoint("POST", "/api/v1/payments", requires_auth=True),
            _endpoint("GET", "/api/v1/docs"),
        ]
    )

    assert plan.modules
    for module in plan.modules:
        assert module.rationale.strip(), f"{module.name} has a priority with no stated reason"


def test_required_checks_match_the_rules_that_will_actually_run() -> None:
    endpoint = _endpoint("GET", "/api/v1/orders/{order_id}", requires_auth=True)
    plan = build_plan([endpoint])

    module = next(m for m in plan.modules if m.name == "orders")
    planned = {c.rule for c in module.checks}

    assert planned == set(applicable_rules(endpoint))
    # The plan is shown to humans, so every check needs a legible intent.
    assert all(c.intent and c.intent != c.rule for c in module.checks)


# ------------------------------------------------------------------ budget order


def test_case_budget_is_spent_on_the_critical_module_first() -> None:
    endpoints = [
        _endpoint("GET", "/api/v1/status"),
        _endpoint("GET", "/api/v1/docs"),
        _endpoint("POST", "/api/v1/auth/login"),
    ]
    plan = build_plan(endpoints)

    report = generate(endpoints, max_cases=1, plan=plan)

    assert len(report.cases) == 1
    assert report.cases[0].endpoint_key == "POST /api/v1/auth/login"


def test_without_a_plan_generation_keeps_its_previous_order() -> None:
    """The plan is additive. Passing none must not change existing behaviour."""
    endpoints = [
        _endpoint("GET", "/api/v1/status"),
        _endpoint("POST", "/api/v1/auth/login"),
    ]

    report = generate(endpoints, max_cases=1)

    assert report.cases[0].endpoint_key == "GET /api/v1/status"


# ---------------------------------------------------------------------- coverage


def test_full_coverage_when_nothing_is_truncated() -> None:
    endpoints = [
        _endpoint("POST", "/api/v1/auth/login"),
        _endpoint("GET", "/api/v1/orders/{order_id}", requires_auth=True),
    ]
    plan = build_plan(endpoints)

    result = coverage(plan, generate(endpoints, plan=plan))

    assert result.missing == 0
    assert result.ratio == 1.0
    assert result.uncovered_modules == []


def test_truncation_is_reported_rather_than_silent() -> None:
    endpoints = [
        _endpoint("POST", "/api/v1/auth/login"),
        _endpoint("GET", "/api/v1/products/{product_id}"),
    ]
    plan = build_plan(endpoints)

    result = coverage(plan, generate(endpoints, max_cases=1, plan=plan))

    assert result.missing > 0
    assert result.ratio < 1.0
    # The module that lost out is named, not merely counted.
    assert "products" in result.uncovered_modules
    assert result.by_module["products"]["generated"] == 0
    assert result.by_module["auth"]["generated"] >= 1


def test_coverage_matches_on_rule_not_on_case_name() -> None:
    """Renaming a case must not fake coverage, or the report is worthless."""
    endpoints = [_endpoint("POST", "/api/v1/auth/login")]
    plan = build_plan(endpoints)
    report = generate(endpoints, plan=plan)

    for case in report.cases:
        case.name = "totally different wording"

    assert coverage(plan, report).missing == 0

    for case in report.cases:
        case.generated_by_rule = ""

    assert coverage(plan, report).generated == 0


# ------------------------------------------------------------------- enrichment


class _StubLlm:
    """Stands in for LlmClient. `available` True, one canned response."""

    def __init__(self, data: dict) -> None:
        self._data = data
        self.available = True
        self.calls: list[str] = []

    def try_complete_json(self, **kwargs) -> dict:
        self.calls.append(kwargs["purpose"])
        return self._data


def test_enrichment_can_raise_a_priority() -> None:
    plan = build_plan([_endpoint("GET", "/api/v1/reports")])
    before = next(m for m in plan.modules if m.name == "reports").priority
    assert before is Priority.LOW

    llm = _StubLlm({"modules": [{"name": "reports", "raise_to": "critical", "rationale": "PII"}]})
    enrich_plan(plan, llm)  # type: ignore[arg-type]

    module = next(m for m in plan.modules if m.name == "reports")
    assert module.priority is Priority.CRITICAL
    assert "PII" in module.rationale


def test_enrichment_cannot_lower_a_priority() -> None:
    """The rules are the floor. A hostile repo must not talk the planner out of
    testing the auth module."""
    plan = build_plan([_endpoint("POST", "/api/v1/auth/login")])
    assert next(m for m in plan.modules if m.name == "auth").priority is Priority.CRITICAL

    llm = _StubLlm({"modules": [{"name": "auth", "raise_to": "high"}]})
    enrich_plan(plan, llm)  # type: ignore[arg-type]

    assert next(m for m in plan.modules if m.name == "auth").priority is Priority.CRITICAL


def test_enrichment_ignores_modules_it_invented() -> None:
    plan = build_plan([_endpoint("GET", "/api/v1/reports")])

    llm = _StubLlm({"modules": [{"name": "not-a-real-module", "raise_to": "critical"}]})
    enrich_plan(plan, llm)  # type: ignore[arg-type]

    assert [m.name for m in plan.modules] == ["reports"]


def test_enrichment_survives_junk_in_the_response() -> None:
    plan = build_plan([_endpoint("GET", "/api/v1/reports")])

    llm = _StubLlm({"modules": ["not a dict", {"name": "reports", "raise_to": "nonsense"}]})
    enrich_plan(plan, llm)  # type: ignore[arg-type]

    assert next(m for m in plan.modules if m.name == "reports").priority is Priority.LOW


def test_enrichment_is_a_no_op_without_a_real_provider() -> None:
    """The default install has no provider configured; planning must still work."""
    plan = build_plan([_endpoint("GET", "/api/v1/reports")])
    llm = LlmClient.from_settings()
    assert not llm.available

    enrich_plan(plan, llm)

    assert next(m for m in plan.modules if m.name == "reports").priority is Priority.LOW
    assert llm.totals()["calls"] == 0
