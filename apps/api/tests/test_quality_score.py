"""The overall quality score.

Most of these tests are about what the score refuses to do. A single number
summarising a codebase is the easiest thing here to do badly: done badly it
moves for reasons nobody can trace, people stop trusting it, and it becomes
decoration.
"""

from __future__ import annotations

from qagent.modules.quality.score import WEIGHTS, compute, render


def test_a_perfect_project_scores_100() -> None:
    score = compute(
        defects={},
        security_findings={},
        coverage_ratio=1.0,
        total_checks=50,
        performance_scenarios=3,
        failing_performance_scenarios=0,
    )

    assert score.score == 100
    assert score.grade == "healthy"


def test_nothing_measured_scores_zero_rather_than_100() -> None:
    """An empty project must not look perfect."""
    score = compute()

    assert score.score == 0
    assert score.measured == []
    assert set(score.to_dict()["not_measured"]) == set(WEIGHTS)


# ------------------------------------------------------ unmeasured is not zero


def test_an_unmeasured_dimension_costs_nothing() -> None:
    """Penalising the absence of a signal makes the score reward running
    scanners rather than fixing defects."""
    with_security = compute(defects={}, security_findings={}, coverage_ratio=1.0)
    without_security = compute(defects={}, coverage_ratio=1.0)

    assert with_security.score == 100
    assert without_security.score == 100


def test_the_score_is_renormalised_over_what_ran() -> None:
    api_only = compute(defects={"high": 1}, coverage_ratio=1.0, total_checks=10)

    assert "security" in api_only.to_dict()["not_measured"]
    assert "performance" in api_only.to_dict()["not_measured"]
    # Still a real number, not a partial one that looks like a bad score.
    assert 0 < api_only.score < 100


def test_a_clean_security_scan_differs_from_no_scan() -> None:
    scanned = compute(defects={"critical": 1}, security_findings={})
    unscanned = compute(defects={"critical": 1})

    assert "security" not in scanned.to_dict()["not_measured"]
    assert "security" in unscanned.to_dict()["not_measured"]


# -------------------------------------------------------------------- defects


def test_one_critical_defect_dominates() -> None:
    """It dominates the decision about whether to ship, so it should dominate
    the number that summarises that decision."""
    clean = compute(defects={}, coverage_ratio=1.0)
    critical = compute(defects={"critical": 1}, coverage_ratio=1.0)

    assert clean.score - critical.score >= 25


def test_severity_is_ordered() -> None:
    scores = [
        compute(defects={severity: 1}, coverage_ratio=1.0).score
        for severity in ("low", "medium", "high", "critical")
    ]

    assert scores == sorted(scores, reverse=True)


def test_informational_defects_cost_nothing() -> None:
    assert compute(defects={"info": 20}, coverage_ratio=1.0).score == compute(
        defects={}, coverage_ratio=1.0
    ).score


def test_the_score_never_goes_below_zero() -> None:
    score = compute(defects={"critical": 50}, security_findings={"critical": 50})

    assert score.score == 0
    assert score.grade == "critical"


# ------------------------------------------------------------------- coverage


def test_low_coverage_costs_points_even_with_no_defects() -> None:
    """Green means 'nothing that ran is wrong', not 'nothing is wrong'."""
    full = compute(defects={}, coverage_ratio=1.0)
    quarter = compute(defects={}, coverage_ratio=0.25)

    assert full.score > quarter.score


# ---------------------------------------------------------------- reliability


def test_flakiness_is_capped_well_below_its_weight() -> None:
    """A score driven down by flaky tests pushes teams to delete tests."""
    every_check_flaky = compute(defects={}, coverage_ratio=1.0, total_checks=10, flaky_checks=10)

    reliability = next(
        d for d in every_check_flaky.dimensions if d.name == "reliability"
    )
    assert reliability.lost < WEIGHTS["reliability"]
    assert every_check_flaky.score > 80


def test_errored_checks_count_toward_reliability_not_defects() -> None:
    score = compute(defects={}, coverage_ratio=1.0, total_checks=10, errored_checks=5)

    defects = next(d for d in score.dimensions if d.name == "defects")
    reliability = next(d for d in score.dimensions if d.name == "reliability")
    assert defects.lost == 0
    assert reliability.lost > 0


# ------------------------------------------------------------- explainability


def test_every_point_lost_is_attributable() -> None:
    """'87/100' alone is not useful; the breakdown is the product."""
    score = compute(
        defects={"high": 2}, security_findings={"medium": 1}, coverage_ratio=0.75
    )
    payload = score.to_dict()

    total_lost = sum(d["points_lost"] for d in payload["dimensions"])
    assert total_lost > 0
    for dimension in payload["dimensions"]:
        assert dimension["detail"]


def test_it_says_what_to_fix_first() -> None:
    score = compute(
        defects={"critical": 1}, security_findings={"low": 1}, coverage_ratio=0.99
    )

    assert score.to_dict()["fix_first"][0] == "defects"


def test_a_clean_project_has_nothing_to_fix_first() -> None:
    assert compute(defects={}, coverage_ratio=1.0).to_dict()["fix_first"] == []


def test_grades_track_the_score() -> None:
    assert compute(defects={}, coverage_ratio=1.0).grade == "healthy"
    assert compute(defects={"critical": 2}, coverage_ratio=0.2).grade in {"at risk", "critical"}


def test_the_payload_explains_how_it_was_computed() -> None:
    assert "renormalised" in compute(defects={}).to_dict()["explains"]


# ---------------------------------------------------------------------- render


def test_render_produces_the_claude_md_panel() -> None:
    text = render(compute(defects={"high": 1}, coverage_ratio=0.8, total_checks=10))

    assert "QA HEALTH" in text
    assert "Overall Score" in text
    assert "/100" in text


def test_render_marks_unmeasured_dimensions_plainly() -> None:
    text = render(compute(defects={}))

    assert "not measured" in text


def test_an_unmeasured_project_is_unknown_not_critical() -> None:
    """A brand new project reading "0/100 critical" on its first page load
    teaches the user to distrust the number before it has told them anything."""
    score = compute()

    assert score.score == 0
    assert score.grade == "not measured"


def test_a_measured_project_scoring_zero_really_is_critical() -> None:
    score = compute(defects={"critical": 20}, coverage_ratio=0.0)

    assert score.score == 0
    assert score.grade == "critical"
