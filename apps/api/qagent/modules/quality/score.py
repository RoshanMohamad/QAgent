"""The overall quality score CLAUDE.md section 4 puts at the top of the dashboard.

A single number summarising a codebase's health is the easiest thing in this
whole project to do badly. Done badly it is a vanity metric: people learn the
number moves for reasons they cannot trace, stop trusting it, and it becomes
decoration on a dashboard nobody reads.

So three rules shape this:

**Every point lost is attributable.** The score is 100 minus a set of named
penalties, and the breakdown is returned alongside it. "87/100" on its own is
not useful; "87/100, -8 for two high-severity defects, -5 for 25% of the API
surface untested" tells someone what to do next.

**Nothing is scored that was not measured.** A project with no security scan
does not lose points for security - it is marked `not_measured` and the
remaining weights are renormalised. Penalising the absence of a signal makes
the score reward running scanners rather than fixing defects, and a team that
cannot run ZAP would watch their score sit low forever and stop looking.

**The weights are stated, not hidden.** They are a judgement call and there is
no objectively correct set; writing them down where they can be argued with is
the honest form of that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: What each dimension can cost, out of 100. Open defects dominate on purpose:
#: this is a defect-finding tool, and a score where "we ran a load test" offsets
#: "there are two critical bugs open" would be measuring the wrong thing.
WEIGHTS = {
    "defects": 40,
    "security": 25,
    "coverage": 20,
    "reliability": 10,
    "performance": 5,
}

#: Penalty per open defect, by severity, as a fraction of the `defects` weight.
#: A single critical exhausts most of it: one critical defect should visibly
#: dominate the score, because it dominates the decision about whether to ship.
DEFECT_COST = {"critical": 0.5, "high": 0.25, "medium": 0.08, "low": 0.02, "info": 0.0}
FINDING_COST = {"critical": 0.5, "high": 0.25, "medium": 0.08, "low": 0.02, "info": 0.0}

#: Flakiness is capped well below its weight: flaky tests are a real problem but
#: they are the suite's problem, not the application's, and a score driven down
#: by them would push teams to delete tests rather than fix them.
MAX_FLAKE_PENALTY = 0.6


@dataclass
class Dimension:
    name: str
    weight: int
    #: 0.0 (worst) to 1.0 (perfect). None when nothing was measured.
    ratio: float | None
    detail: str

    @property
    def measured(self) -> bool:
        return self.ratio is not None

    @property
    def earned(self) -> float:
        return 0.0 if self.ratio is None else self.weight * self.ratio

    @property
    def lost(self) -> float:
        return 0.0 if self.ratio is None else self.weight * (1.0 - self.ratio)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "weight": self.weight,
            "measured": self.measured,
            "ratio": None if self.ratio is None else round(self.ratio, 3),
            "points_lost": round(self.lost, 1),
            "detail": self.detail,
        }


@dataclass
class QualityScore:
    dimensions: list[Dimension] = field(default_factory=list)

    @property
    def measured(self) -> list[Dimension]:
        return [d for d in self.dimensions if d.measured]

    @property
    def score(self) -> int:
        """0-100, renormalised over whatever was actually measured.

        A project that only ran API checks is scored out of the dimensions it
        exercised, not marked down for the ones it never ran.
        """
        available = sum(d.weight for d in self.measured)
        if not available:
            return 0
        earned = sum(d.earned for d in self.measured)
        return max(0, min(100, round(100 * earned / available)))

    @property
    def grade(self) -> str:
        """The label a human reads, which is not always a function of the score.

        A project where nothing has been measured scores 0 - correctly, since
        an unmeasured project must not look perfect - but grading it "critical"
        would be a different lie: it is not at risk, it is *unknown*. A brand
        new project reading "0/100 critical" on its first page load teaches the
        user to distrust the number before it has told them anything.
        """
        if not self.measured:
            return "not measured"

        score = self.score
        if score >= 90:
            return "healthy"
        if score >= 75:
            return "watch"
        if score >= 50:
            return "at risk"
        return "critical"

    def biggest_losses(self, limit: int = 3) -> list[Dimension]:
        """What to fix first. The score's only actionable output."""
        return sorted(self.measured, key=lambda d: -d.lost)[:limit]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "grade": self.grade,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "not_measured": [d.name for d in self.dimensions if not d.measured],
            "fix_first": [d.name for d in self.biggest_losses() if d.lost > 0],
            "explains": (
                "100 minus named penalties, renormalised over the dimensions that "
                "were actually measured. Unmeasured dimensions cost nothing."
            ),
        }


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _severity_ratio(counts: dict[str, int], costs: dict[str, float]) -> float:
    penalty = sum(costs.get(severity, 0.0) * n for severity, n in (counts or {}).items())
    return _clamp(1.0 - penalty)


def compute(
    *,
    defects: dict[str, int] | None = None,
    security_findings: dict[str, int] | None = None,
    coverage_ratio: float | None = None,
    total_checks: int = 0,
    flaky_checks: int = 0,
    errored_checks: int = 0,
    performance_scenarios: int = 0,
    failing_performance_scenarios: int = 0,
) -> QualityScore:
    """Build the score from what a project has actually measured.

    Every argument is optional and ``None``/zero means "not measured", not
    "perfect" - except where a zero genuinely is a measurement (no open defects
    after a run that ran checks).
    """
    result = QualityScore()

    # --- defects ---------------------------------------------------------
    if defects is None:
        result.dimensions.append(
            Dimension("defects", WEIGHTS["defects"], None, "no run has reported defects yet")
        )
    else:
        ratio = _severity_ratio(defects, DEFECT_COST)
        blocking = defects.get("critical", 0) + defects.get("high", 0)
        result.dimensions.append(
            Dimension(
                "defects",
                WEIGHTS["defects"],
                ratio,
                f"{sum(defects.values())} open ({blocking} at high or critical)",
            )
        )

    # --- security --------------------------------------------------------
    if security_findings is None:
        result.dimensions.append(
            Dimension("security", WEIGHTS["security"], None, "no security scan has run")
        )
    else:
        ratio = _severity_ratio(security_findings, FINDING_COST)
        result.dimensions.append(
            Dimension(
                "security",
                WEIGHTS["security"],
                ratio,
                f"{sum(security_findings.values())} open finding(s)",
            )
        )

    # --- coverage --------------------------------------------------------
    if coverage_ratio is None:
        result.dimensions.append(
            Dimension("coverage", WEIGHTS["coverage"], None, "nothing discovered to cover")
        )
    else:
        result.dimensions.append(
            Dimension(
                "coverage",
                WEIGHTS["coverage"],
                _clamp(coverage_ratio),
                f"{round(coverage_ratio * 100)}% of the discovered API surface exercised",
            )
        )

    # --- reliability -----------------------------------------------------
    # Flaky and errored checks: the suite's own health, not the application's.
    if total_checks <= 0:
        result.dimensions.append(
            Dimension("reliability", WEIGHTS["reliability"], None, "no checks have run")
        )
    else:
        unreliable = (flaky_checks + errored_checks) / total_checks
        ratio = _clamp(1.0 - min(unreliable, MAX_FLAKE_PENALTY))
        result.dimensions.append(
            Dimension(
                "reliability",
                WEIGHTS["reliability"],
                ratio,
                f"{flaky_checks} flaky, {errored_checks} errored of {total_checks}",
            )
        )

    # --- performance -----------------------------------------------------
    if performance_scenarios <= 0:
        result.dimensions.append(
            Dimension("performance", WEIGHTS["performance"], None, "no load test has run")
        )
    else:
        ratio = _clamp(1.0 - failing_performance_scenarios / performance_scenarios)
        result.dimensions.append(
            Dimension(
                "performance",
                WEIGHTS["performance"],
                ratio,
                f"{failing_performance_scenarios} of {performance_scenarios} scenario(s) "
                f"over threshold",
            )
        )

    logger.info(
        "quality score %d/100 (%s), %d dimension(s) measured",
        result.score,
        result.grade,
        len(result.measured),
    )
    return result


def render(score: QualityScore) -> str:
    """The CLAUDE.md section 4 panel, as plain text."""
    lines = [
        "QA HEALTH",
        "",
        f"Overall Score                 {score.score}/100  ({score.grade})",
        "",
    ]

    width = max((len(d.name) for d in score.dimensions), default=12)
    for dimension in score.dimensions:
        if not dimension.measured:
            lines.append(f"  {dimension.name:<{width}}  not measured   {dimension.detail}")
            continue
        lines.append(
            f"  {dimension.name:<{width}}  -{dimension.lost:>4.1f} pts     {dimension.detail}"
        )

    worst = [d for d in score.biggest_losses() if d.lost > 0]
    if worst:
        lines.append("")
        lines.append("Fix first: " + ", ".join(d.name for d in worst))
    return "\n".join(lines)
