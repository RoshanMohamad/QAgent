"""Surface coverage: honest numbers for a black-box tool.

The headline assertion in this file is negative - coverage is credited only for
endpoints a check actually ran against, never for ones merely discovered or
merely planned. A coverage metric that inflates is worse than none.
"""

from __future__ import annotations

from datetime import UTC, datetime

from qagent.modules.coverage.surface import (
    endpoint_coverage,
    route_coverage,
    summarise,
)
from qagent.modules.discovery.openapi import EndpointSpec
from qagent.pipeline import CaseOutcome, PipelineResult

# ---------------------------------------------------------------- endpoints


def test_only_exercised_endpoints_count() -> None:
    coverage = endpoint_coverage(
        ["GET /orders", "POST /orders", "GET /health"],
        exercised={"GET /orders"},
    )

    assert coverage.total == 3
    assert coverage.covered == 1
    assert coverage.percent == 33


def test_the_untested_endpoints_are_named() -> None:
    """The percentage is the headline; this list is the actual next action."""
    coverage = endpoint_coverage(
        ["GET /orders", "POST /payments"], exercised={"GET /orders"}
    )

    assert coverage.uncovered == ["POST /payments"]


def test_duplicate_discoveries_are_counted_once() -> None:
    coverage = endpoint_coverage(
        ["GET /orders", "GET /orders"], exercised={"GET /orders"}
    )

    assert coverage.total == 1
    assert coverage.ratio == 1.0


def test_an_empty_surface_is_fully_covered() -> None:
    """A project with no frontend must not look like one with an untested frontend."""
    coverage = endpoint_coverage([], exercised=set())

    assert coverage.ratio == 1.0
    assert coverage.percent == 100


def test_coverage_is_broken_down_by_module() -> None:
    coverage = endpoint_coverage(
        [
            "POST /api/v1/auth/login",
            "GET /api/v1/auth/session",
            "GET /api/v1/health",
        ],
        exercised={"POST /api/v1/auth/login"},
    )

    assert coverage.by_module["auth"] == {"total": 2, "covered": 1, "percent": 50}
    assert coverage.by_module["health"]["percent"] == 0


def test_uncovered_list_is_bounded_in_the_payload() -> None:
    keys = [f"GET /r{i}" for i in range(200)]

    payload = endpoint_coverage(keys, exercised=set()).to_dict()

    assert len(payload["uncovered"]) == 50
    assert payload["uncovered_total"] == 200


# ------------------------------------------------------------------- routes


def test_a_visited_route_is_covered() -> None:
    coverage = route_coverage(["/", "/products"], ["http://app/", "http://app/products"])

    assert coverage.ratio == 1.0


def test_query_strings_and_trailing_slashes_do_not_defeat_matching() -> None:
    coverage = route_coverage(["/products"], ["http://app/products/?ref=email"])

    assert coverage.covered == 1


def test_dynamic_routes_match_a_concrete_url() -> None:
    """`/products/[id]` is never visited literally."""
    for declared in ("/products/[id]", "/products/{id}", "/products/:id"):
        coverage = route_coverage([declared], ["http://app/products/42"])
        assert coverage.covered == 1, declared


def test_a_dynamic_route_does_not_match_a_deeper_path() -> None:
    coverage = route_coverage(["/products/[id]"], ["http://app/products/42/reviews"])

    assert coverage.covered == 0


def test_unvisited_routes_are_reported() -> None:
    coverage = route_coverage(["/", "/checkout"], ["http://app/"])

    assert coverage.uncovered == ["/checkout"]


# ---------------------------------------------------------------- summarise


def _result(endpoints: list[str], outcomes: list[CaseOutcome]) -> PipelineResult:
    result = PipelineResult(base_url="http://app", started_at=datetime.now(UTC))
    result.endpoints = [
        EndpointSpec(method=k.split(" ")[0], path=k.split(" ", 1)[1]) for k in endpoints
    ]
    result.outcomes = outcomes
    result.finished_at = datetime.now(UTC)
    return result


def _outcome(endpoint_key: str | None, kind: str = "api_functional", path: str = "") -> CaseOutcome:
    return CaseOutcome(
        name="c",
        kind=kind,
        endpoint_key=endpoint_key,
        status="passed",
        duration_ms=1,
        request={"method": "GET", "path": path},
        response={},
        assertions=[],
    )


def test_summary_reflects_what_actually_ran() -> None:
    result = _result(
        ["GET /orders", "POST /orders", "GET /health"],
        [_outcome("GET /orders"), _outcome("POST /orders")],
    )

    report = summarise(result)

    assert report.endpoints.covered == 2
    assert report.endpoints.uncovered == ["GET /health"]


def test_a_truncated_run_does_not_report_coverage_it_did_not_earn() -> None:
    """--max-cases stopping early must show as missing surface, not as success."""
    result = _result(["GET /a", "GET /b", "GET /c"], [_outcome("GET /a")])

    assert summarise(result).endpoints.percent == 33


def test_browser_outcomes_contribute_route_coverage() -> None:
    result = _result(
        ["GET /orders"],
        [
            _outcome("GET /orders"),
            _outcome(None, kind="e2e", path="http://app/checkout"),
        ],
    )

    report = summarise(result)

    assert report.routes is not None
    assert report.routes.covered == 1


def test_no_browser_stage_means_no_route_figure() -> None:
    """Reporting 0% route coverage for an API-only run would be a false alarm."""
    report = summarise(_result(["GET /orders"], [_outcome("GET /orders")]))

    assert report.routes is None
    assert report.to_dict()["route_surface"] is None


def test_the_payload_says_what_it_measures() -> None:
    """A label reading '82%' that silently is not line coverage would be read as
    a claim about tested code paths."""
    payload = summarise(_result(["GET /a"], [_outcome("GET /a")])).to_dict()

    assert "Not line coverage" in payload["measures"]
    assert "endpoint_surface" in payload
