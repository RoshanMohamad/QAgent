"""Rule-based test generation.

Every case produced here is derived deterministically from a discovered endpoint, so
generation is reproducible, free, and explainable to the developer reading the report.
The LLM layer (see enrich.py) only ever *adds* cases on top of these; it can never
remove or weaken one.

The generated document is declarative rather than emitted code. That choice matters:
a declarative spec is diffable, reviewable, safe to execute without eval, and can be
rendered to Playwright or pytest later without regenerating anything.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from qagent.modules.discovery.openapi import EndpointSpec
from qagent.modules.generator import values

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from qagent.modules.planner.strategy import TestPlan

#: A 5xx is never a correct answer to a malformed request. Every negative case
#: asserts this, and it is the assertion that finds real defects most often.
NO_SERVER_ERROR = {"type": "status_not_in", "value": [500, 502, 503, 504]}

#: Stack traces leaking to clients are both an information disclosure and a reliable
#: signal that an unhandled exception occurred.
NO_STACK_TRACE = {
    "type": "body_not_matches",
    "value": r"(Traceback \(most recent call last\)|at [\w.$]+\(.*\.java:\d+\)|\bstack trace\b)",
}


@dataclass
class GeneratedCase:
    name: str
    kind: str
    spec: dict
    generated_by: str = "rule"
    endpoint_key: str | None = None
    rationale: str = ""
    #: Which rule function produced this, e.g. ``case_unauthenticated``. The
    #: planner promises coverage in terms of ``(endpoint_key, rule)`` pairs, so
    #: this is what lets it verify afterwards that the promise was kept.
    #: Empty for anything a rule did not produce (LLM-added cases).
    generated_by_rule: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GenerationReport:
    cases: list[GeneratedCase] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def add(self, case: GeneratedCase) -> None:
        self.cases.append(case)


def _request(
    endpoint: EndpointSpec,
    *,
    path_params: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
    auth: str = "default",
) -> dict:
    return {
        "method": endpoint.method,
        "path": endpoint.path,
        "path_params": path_params or {},
        "query": query or {},
        "json": body,
        "auth": auth,
    }


def _valid_path_params(endpoint: EndpointSpec) -> dict[str, str]:
    return {
        p["name"]: values.path_param_value(p, valid=True)
        for p in endpoint.path_params
        if p.get("name")
    }


def _required_query(endpoint: EndpointSpec) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for param in endpoint.parameters:
        if param.get("in") == "query" and param.get("required"):
            out[param["name"]] = values.example_for(param.get("schema", {}), param.get("name", "q"))
    return out


# --------------------------------------------------------------------------- rules


def case_happy_path(endpoint: EndpointSpec) -> GeneratedCase:
    body = values.example_body(endpoint.request_schema) if endpoint.request_schema else None
    expected = endpoint.success_status
    return GeneratedCase(
        name=f"{endpoint.key()} returns {expected} for a valid request",
        kind="api_functional",
        endpoint_key=endpoint.key(),
        rationale=(
            "Baseline: the documented success path must work before anything else is meaningful."
        ),
        spec={
            "request": _request(
                endpoint,
                path_params=_valid_path_params(endpoint),
                query=_required_query(endpoint),
                body=body,
            ),
            "assertions": [
                {"type": "status_in", "value": sorted({expected, 200, 201, 202, 204})},
                NO_STACK_TRACE,
            ],
            "expectation": f"A well-formed request succeeds with {expected}.",
        },
    )


def case_missing_required_field(endpoint: EndpointSpec) -> GeneratedCase | None:
    """Omit one required body field.

    This is the CLAUDE.md section 13 scenario: the handler assumes a field is present
    and dereferences it, producing 500 where 400 was specified.
    """
    required = endpoint.required_body_fields
    if not required:
        return None

    dropped = required[0]
    body = values.example_body(endpoint.request_schema)
    body.pop(dropped, None)

    return GeneratedCase(
        name=f"{endpoint.key()} rejects a body missing '{dropped}'",
        kind="api_functional",
        endpoint_key=endpoint.key(),
        rationale=(
            f"'{dropped}' is declared required; omitting it must be a client error, not a crash."
        ),
        spec={
            "request": _request(
                endpoint,
                path_params=_valid_path_params(endpoint),
                query=_required_query(endpoint),
                body=body,
            ),
            "assertions": [
                {"type": "status_in", "value": [400, 409, 422]},
                NO_SERVER_ERROR,
                NO_STACK_TRACE,
            ],
            "expectation": f"Omitting required field '{dropped}' returns a 4xx validation error.",
        },
    )


def case_wrong_field_type(endpoint: EndpointSpec) -> GeneratedCase | None:
    schema = endpoint.request_schema
    if not schema or not schema.get("properties"):
        return None

    props = schema["properties"]
    target = (endpoint.required_body_fields or list(props))[0]
    body = values.example_body(schema)
    body[target] = values.violation_for(props.get(target, {}), target)

    return GeneratedCase(
        name=f"{endpoint.key()} rejects a wrongly typed '{target}'",
        kind="api_functional",
        endpoint_key=endpoint.key(),
        rationale="Type confusion in a handler is a common source of unhandled exceptions.",
        spec={
            "request": _request(
                endpoint,
                path_params=_valid_path_params(endpoint),
                query=_required_query(endpoint),
                body=body,
            ),
            "assertions": [
                {"type": "status_in", "value": [400, 409, 422]},
                NO_SERVER_ERROR,
                NO_STACK_TRACE,
            ],
            "expectation": (
                f"A value of the wrong type for '{target}' returns a 4xx validation error."
            ),
        },
    )


def case_malformed_path_param(endpoint: EndpointSpec) -> GeneratedCase | None:
    params = endpoint.path_params
    if not params:
        return None

    target = params[0]
    path_params = _valid_path_params(endpoint)
    path_params[target["name"]] = values.path_param_value(target, valid=False)

    return GeneratedCase(
        name=f"{endpoint.key()} rejects a malformed '{target['name']}'",
        kind="api_functional",
        endpoint_key=endpoint.key(),
        rationale="Identifiers are routinely cast without guarding, turning bad input into a 500.",
        spec={
            "request": _request(
                endpoint,
                path_params=path_params,
                query=_required_query(endpoint),
                body=values.example_body(endpoint.request_schema)
                if endpoint.request_schema
                else None,
            ),
            "assertions": [
                {"type": "status_in", "value": [400, 404, 422]},
                NO_SERVER_ERROR,
                NO_STACK_TRACE,
            ],
            "expectation": (
                f"A malformed '{target['name']}' returns 400 or 404, never a server error."
            ),
        },
    )


def case_absent_resource(endpoint: EndpointSpec) -> GeneratedCase | None:
    params = endpoint.path_params
    if not params:
        return None

    target = params[0]
    path_params = _valid_path_params(endpoint)
    path_params[target["name"]] = values.absent_resource_id(target)

    return GeneratedCase(
        name=f"{endpoint.key()} returns 404 for an absent resource",
        kind="api_functional",
        endpoint_key=endpoint.key(),
        rationale=(
            "A well-formed identifier for a row that does not exist must not crash the handler."
        ),
        spec={
            "request": _request(
                endpoint,
                path_params=path_params,
                query=_required_query(endpoint),
                body=values.example_body(endpoint.request_schema)
                if endpoint.request_schema
                else None,
            ),
            "assertions": [
                {"type": "status_in", "value": [400, 403, 404, 410]},
                NO_SERVER_ERROR,
                NO_STACK_TRACE,
            ],
            "expectation": "A valid-looking but non-existent id returns 404.",
        },
    )


def case_unauthenticated(endpoint: EndpointSpec) -> GeneratedCase | None:
    """Strip credentials from a protected endpoint.

    This is the highest-value security check that can be generated from a spec alone,
    and unlike most scanner output it has almost no false-positive surface.
    """
    if not endpoint.requires_auth:
        return None

    return GeneratedCase(
        name=f"{endpoint.key()} requires authentication",
        kind="api_security",
        endpoint_key=endpoint.key(),
        rationale="The specification marks this operation as protected.",
        spec={
            "request": _request(
                endpoint,
                path_params=_valid_path_params(endpoint),
                query=_required_query(endpoint),
                body=values.example_body(endpoint.request_schema)
                if endpoint.request_schema
                else None,
                auth="none",
            ),
            "assertions": [
                {"type": "status_in", "value": [401, 403]},
                NO_SERVER_ERROR,
            ],
            "expectation": "An unauthenticated request is rejected with 401 or 403.",
        },
    )


def case_invalid_token(endpoint: EndpointSpec) -> GeneratedCase | None:
    if not endpoint.requires_auth:
        return None

    return GeneratedCase(
        name=f"{endpoint.key()} rejects a forged bearer token",
        kind="api_security",
        endpoint_key=endpoint.key(),
        rationale="A structurally valid but unsigned token must not be accepted.",
        spec={
            "request": {
                **_request(
                    endpoint,
                    path_params=_valid_path_params(endpoint),
                    query=_required_query(endpoint),
                    body=values.example_body(endpoint.request_schema)
                    if endpoint.request_schema
                    else None,
                    auth="none",
                ),
                "headers": {
                    "Authorization": (
                        "Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
                        "eyJzdWIiOiJxYWdlbnQtcHJvYmUiLCJyb2xlIjoiYWRtaW4ifQ."
                    )
                },
            },
            "assertions": [
                {"type": "status_in", "value": [401, 403]},
                NO_SERVER_ERROR,
            ],
            "expectation": "An alg=none token is rejected with 401 or 403.",
        },
    )


RULES = [
    case_happy_path,
    case_missing_required_field,
    case_wrong_field_type,
    case_malformed_path_param,
    case_absent_resource,
    case_unauthenticated,
    case_invalid_token,
]

#: One line per rule, phrased the way CLAUDE.md section 8's planner example
#: phrases its checklist ("Login success", "Wrong password"). The planner shows
#: these to a human; the rule names above are what it verifies against.
RULE_INTENTS: dict[str, str] = {
    "case_happy_path": "Valid request succeeds",
    "case_missing_required_field": "Missing required field is rejected",
    "case_wrong_field_type": "Wrongly typed field is rejected",
    "case_malformed_path_param": "Malformed identifier is rejected",
    "case_absent_resource": "Absent resource returns 404",
    "case_unauthenticated": "Unauthenticated request is refused",
    "case_invalid_token": "Forged token is refused",
}


def rule_intent(rule_name: str) -> str:
    return RULE_INTENTS.get(rule_name, rule_name)


def applicable_rules(endpoint: EndpointSpec) -> list[str]:
    """Which rules produce a case for this endpoint.

    The planner needs to state required coverage *before* anything executes, and
    the only drift-free way to know what generation will produce is to ask the
    rules themselves. They are pure dict construction with no I/O, so running
    them twice costs nothing measurable and removes an entire class of bug where
    a duplicated applicability predicate quietly disagrees with its rule.
    """
    names = []
    for rule in RULES:
        try:
            if rule(endpoint) is not None:
                names.append(rule.__name__)
        except Exception as exc:  # noqa: BLE001 - a bad rule must not stop planning
            # Worth a log line: a rule that raises here but not in generate()
            # produces a plan that under-promises, which is confusing rather
            # than merely incomplete.
            logger.warning(
                "rule %s raised while planning %s: %s", rule.__name__, endpoint.key(), exc
            )
    return names


def generate(
    endpoints: list[EndpointSpec],
    *,
    max_cases: int | None = None,
    plan: TestPlan | None = None,
) -> GenerationReport:
    """Apply every rule to every endpoint, highest risk first.

    Endpoints arrive pre-sorted by risk, so truncating at ``max_cases`` keeps the
    most valuable coverage rather than an arbitrary alphabetical slice. When a
    ``plan`` is supplied (agent 2, ``modules/planner/strategy.py``) the ordering
    is taken from it instead, which is strictly better: risk score orders
    endpoints individually, whereas the plan orders whole *modules* by priority,
    so a truncated run loses the health check rather than half of the auth
    surface.
    """
    report = GenerationReport()
    ordered = plan.order_endpoints(endpoints) if plan is not None else endpoints

    for endpoint in ordered:
        for rule in RULES:
            if max_cases is not None and len(report.cases) >= max_cases:
                report.skipped.append(f"{endpoint.key()} (case limit reached)")
                return report
            try:
                case = rule(endpoint)
            except Exception as exc:  # noqa: BLE001 - a bad rule must not stop generation
                report.skipped.append(f"{endpoint.key()} via {rule.__name__}: {exc}")
                continue
            if case is not None:
                # Stamped centrally rather than inside each rule: one place to
                # keep correct, and no rule can forget.
                case.generated_by_rule = rule.__name__
                report.add(case)

    return report
