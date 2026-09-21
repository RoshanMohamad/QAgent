"""The quality gate: what blocks a deploy, what deliberately does not, and the
guarantee that a run which tested nothing never reports success.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from qagent.cli import app
from qagent.modules.gate.policy import (
    GatePolicy,
    count_severities,
    evaluate,
    render,
)
from qagent.modules.planner.strategy import CoverageReport
from qagent.pipeline import CaseOutcome, PipelineResult

runner = CliRunner()


def _outcome(status: str = "failed", severity: str | None = None, klass: str = "real_bug"):
    return CaseOutcome(
        name="check",
        kind="api_functional",
        endpoint_key="GET /x",
        status=status,
        duration_ms=1,
        request={},
        response={},
        assertions=[],
        verdict={"failure_class": klass, "confidence": 0.9},
        bug={"title": "t", "severity": severity} if severity else None,
    )


def _result(outcomes=None, coverage: CoverageReport | None = None) -> PipelineResult:
    result = PipelineResult(base_url="http://x", started_at=datetime.now(UTC))
    result.outcomes = outcomes or []
    result.coverage = coverage
    result.finished_at = datetime.now(UTC)
    return result


# --------------------------------------------------------------------- counting


def test_only_classified_defects_have_a_severity() -> None:
    """A flaky failure is not a defect, so it carries no severity and no weight."""
    result = _result(
        [
            _outcome(severity="high"),
            _outcome(severity=None, klass="flaky_test"),
            _outcome(severity=None, klass="environment"),
        ]
    )

    assert count_severities(result) == {"high": 1}


def test_severities_are_counted_case_insensitively() -> None:
    result = _result([_outcome(severity="HIGH"), _outcome(severity="high")])

    assert count_severities(result) == {"high": 2}


# --------------------------------------------------------------------- blocking


def test_a_clean_run_deploys() -> None:
    decision = evaluate(_result([_outcome(status="passed")]))

    assert decision.result == "pass"
    assert decision.exit_code == 0
    assert decision.reasons == []


def test_a_critical_defect_blocks() -> None:
    decision = evaluate(_result([_outcome(severity="critical")]))

    assert decision.blocked
    assert decision.exit_code == 1
    assert any("critical" in r for r in decision.reasons)


def test_a_flaky_failure_does_not_block() -> None:
    """A gate that blocks on infrastructure is a gate nobody leaves enabled."""
    decision = evaluate(_result([_outcome(klass="flaky_test"), _outcome(klass="environment")]))

    assert not decision.blocked


def test_medium_defects_do_not_block_by_default() -> None:
    decision = evaluate(_result([_outcome(severity="medium"), _outcome(severity="low")]))

    assert not decision.blocked


def test_medium_blocks_once_a_threshold_is_set() -> None:
    result = _result([_outcome(severity="medium"), _outcome(severity="medium")])

    assert not evaluate(result, GatePolicy(max_medium=2)).blocked
    assert evaluate(result, GatePolicy(max_medium=1)).blocked


def test_errored_checks_do_not_block_unless_asked() -> None:
    result = _result([_outcome(status="error"), _outcome(status="error")])

    assert not evaluate(result).blocked
    assert evaluate(result, GatePolicy(max_errors=1)).blocked


# --------------------------------------------------------------------- coverage


def _coverage(ratio: float, uncovered: list[str] | None = None) -> CoverageReport:
    planned = 100
    report = CoverageReport(planned=planned, generated=int(planned * ratio))
    report.uncovered_modules = uncovered or []
    return report


def test_low_coverage_blocks_a_run_with_no_defects() -> None:
    """Green means 'nothing that ran is wrong', which is not 'nothing is wrong'."""
    result = _result([_outcome(status="passed")], coverage=_coverage(0.4, ["auth"]))

    decision = evaluate(result, GatePolicy(min_coverage=0.9))

    assert decision.blocked
    assert any("auth" in r for r in decision.reasons)


def test_sufficient_coverage_passes() -> None:
    result = _result([_outcome(status="passed")], coverage=_coverage(0.95))

    assert not evaluate(result, GatePolicy(min_coverage=0.9)).blocked


def test_coverage_is_not_checked_unless_a_minimum_is_set() -> None:
    result = _result([_outcome(status="passed")], coverage=_coverage(0.1))

    assert not evaluate(result).blocked


def test_a_missing_plan_does_not_fail_the_coverage_check() -> None:
    """Discovery already reported that; blaming the gate for it helps nobody."""
    result = _result([_outcome(status="passed")], coverage=None)

    assert not evaluate(result, GatePolicy(min_coverage=1.0)).blocked


# ---------------------------------------------------------------------- report


def test_every_check_reports_threshold_and_actual_even_when_passing() -> None:
    """A gate that prints only failures cannot be tuned."""
    decision = evaluate(_result([_outcome(severity="high")]), GatePolicy(max_high=5))

    check = next(c for c in decision.checks if c.name == "high_defects")
    assert check.passed
    assert check.actual == 1
    assert check.threshold == 5


def test_render_states_the_verdict_plainly() -> None:
    blocked = render(evaluate(_result([_outcome(severity="critical")])))
    clean = render(evaluate(_result([_outcome(status="passed")])))

    assert "RESULT: BLOCK" in blocked
    assert "Reason:" in blocked
    assert "RESULT: DEPLOY" in clean


def test_decision_serialises_for_ci() -> None:
    payload = evaluate(_result([_outcome(severity="high")])).to_dict()

    assert payload["result"] == "block"
    assert payload["policy"]["max_high"] == 0
    assert json.dumps(payload)


# ------------------------------------------------------------------- cli wiring


def _patch_pipeline(monkeypatch, result: PipelineResult) -> None:
    monkeypatch.setattr("qagent.cli.run_pipeline", lambda **kwargs: result)


def test_gate_exits_zero_on_a_clean_run(monkeypatch) -> None:
    _patch_pipeline(monkeypatch, _result([_outcome(status="passed")]))

    assert runner.invoke(app, ["gate", "--url", "http://x"]).exit_code == 0


def test_gate_exits_one_when_blocked(monkeypatch) -> None:
    _patch_pipeline(monkeypatch, _result([_outcome(severity="critical")]))

    result = runner.invoke(app, ["gate", "--url", "http://x"])

    assert result.exit_code == 1
    assert "BLOCK" in result.output


def test_gate_exits_two_when_nothing_was_tested(monkeypatch) -> None:
    """The worst failure mode: waving through a deploy because nothing ran."""
    empty = _result([])
    empty.errors = ["No OpenAPI document could be retrieved"]
    _patch_pipeline(monkeypatch, empty)

    result = runner.invoke(app, ["gate", "--url", "http://x"])

    assert result.exit_code == 2
    assert "ERROR" in result.output


def test_gate_honours_a_relaxed_threshold(monkeypatch) -> None:
    _patch_pipeline(monkeypatch, _result([_outcome(severity="high")]))

    assert runner.invoke(app, ["gate", "--url", "http://x"]).exit_code == 1
    assert runner.invoke(app, ["gate", "--url", "http://x", "--max-high", "1"]).exit_code == 0


def test_gate_writes_json(monkeypatch, tmp_path) -> None:
    _patch_pipeline(monkeypatch, _result([_outcome(severity="critical")]))
    out = tmp_path / "gate.json"

    runner.invoke(app, ["gate", "--url", "http://x", "--json", str(out)])

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["gate"]["result"] == "block"
    assert payload["summary"]["base_url"] == "http://x"
