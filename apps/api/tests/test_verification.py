"""Fix verification replays one defect's check several times and only calls it
fixed when every attempt passes (qagent/verification.py) - no database."""

from __future__ import annotations

import pytest

from qagent.modules.runner.executor import ExecutionOutcome
from qagent.verification import MAX_ATTEMPTS, NotVerifiable, check_verifiable, verify

SPEC = {
    "request": {"method": "POST", "path": "/orders", "json": {"product_id": 999}},
    "assertions": [{"type": "status_in", "expected": [400, 404, 422]}],
}


class ScriptedRunner:
    def __init__(self, statuses: list[str]) -> None:
        self._statuses = list(statuses)
        self.calls: list[dict] = []

    def execute(self, spec: dict) -> ExecutionOutcome:
        self.calls.append(spec)
        status = self._statuses.pop(0)
        return ExecutionOutcome(
            passed=status == "passed",
            status=status,
            duration_ms=1,
            request=spec["request"],
            response={"status": 400 if status == "passed" else 500},
            assertions=[],
        )


@pytest.mark.parametrize(
    ("statuses", "verdict"),
    [
        (["passed", "passed", "passed"], "fixed"),
        (["failed", "failed", "failed"], "still_failing"),
        (["passed", "failed", "passed"], "inconclusive"),
        (["passed", "passed", "error"], "inconclusive"),
        (["error", "error", "error"], "inconclusive"),
    ],
)
def test_verdict_requires_every_attempt_to_agree(statuses: list[str], verdict: str) -> None:
    runner = ScriptedRunner(statuses)

    result = verify(runner, SPEC, attempts=3)

    assert result.verdict == verdict
    assert len(runner.calls) == 3
    assert result.summary()["attempts"] == 3


def test_summary_counts_each_status() -> None:
    result = verify(ScriptedRunner(["passed", "failed", "error"]), SPEC, attempts=3)
    assert result.summary() == {
        "verdict": "inconclusive",
        "attempts": 3,
        "passed": 1,
        "failed": 1,
        "errored": 1,
    }


@pytest.mark.parametrize("attempts", [0, MAX_ATTEMPTS + 1])
def test_attempts_are_bounded(attempts: int) -> None:
    with pytest.raises(ValueError):
        verify(ScriptedRunner([]), SPEC, attempts=attempts)


def test_api_check_with_assertions_is_verifiable() -> None:
    assert check_verifiable(kind="api_functional", spec=SPEC) is SPEC


@pytest.mark.parametrize("kind", ["e2e", "e2e_interactive"])
def test_browser_defects_are_not_replayable(kind: str) -> None:
    with pytest.raises(NotVerifiable, match="full scan"):
        check_verifiable(kind=kind, spec=SPEC)


def test_a_case_stored_without_assertions_is_not_verifiable() -> None:
    # What every case persisted before specs carried assertions looks like.
    with pytest.raises(NotVerifiable, match="assertions"):
        check_verifiable(kind="api_functional", spec={"request": SPEC["request"]})


def test_an_idor_finding_needs_two_identities() -> None:
    spec = {
        "request": {"method": "GET", "path": "/tasks/1", "auth": "secondary"},
        "assertions": [{"type": "status_in", "expected": [403]}],
    }
    with pytest.raises(NotVerifiable, match="two identities"):
        check_verifiable(kind="api_security", spec=spec)


def test_pipeline_outcomes_carry_their_executable_spec() -> None:
    """The spec must reach persistence, or no stored case is ever verifiable."""
    from qagent.pipeline import _execute_and_triage

    class Case:
        name = "POST /orders rejects unknown product"
        kind = "api_functional"
        endpoint_key = "POST /orders"
        spec = SPEC

    outcome = _execute_and_triage(
        case=Case(),
        runner=ScriptedRunner(["passed"]),
        llm=None,  # a passing check returns before triage ever consults the model
        auth_configured=False,
        history=[],
        base_url="http://127.0.0.1:1",
    )

    assert outcome.spec == SPEC
