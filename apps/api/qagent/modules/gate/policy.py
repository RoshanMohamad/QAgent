"""The deploy-or-block decision, as data (CLAUDE.md section 18).

`GET /api/v1/projects/{id}/quality` already answered this for a *persisted*
project. This module answers it for a pipeline result with no database at all,
because that is the shape CI needs: a pull request has no project row, no
history and no org, and requiring one would mean standing up Postgres to find
out whether a branch is safe to merge.

Three properties matter more than the arithmetic:

**It blocks on classified defects, not on red tests.** A test that failed
because staging was down is not a reason to stop a deploy, and a gate that
blocks on those is a gate that gets switched off within a fortnight. The triage
stage already separates the two; this consumes that judgement rather than
re-deriving it.

**It blocks on coverage, not only on findings.** Once the planner can say "this
run covered 40% of what it planned", a green result stops meaning "nothing is
wrong" and starts meaning "nothing that ran is wrong". Those are different
claims and only one of them should gate a release.

**Every check reports its threshold and its actual value**, whether it passed or
failed. A gate that prints only what went wrong cannot be tuned, because nobody
can see how close the passing checks were to failing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qagent.pipeline import PipelineResult

logger = logging.getLogger(__name__)

#: Severities that block by default. Medium and below are reported and counted
#: but do not stop a release on their own - a gate that blocks on every `low`
#: finding is one nobody leaves enabled.
BLOCKING_SEVERITIES = ("critical", "high")


@dataclass(frozen=True)
class GatePolicy:
    """Thresholds. Every field is a maximum except ``min_coverage``."""

    max_critical: int = 0
    max_high: int = 0
    max_medium: int | None = None
    max_low: int | None = None

    #: Fraction of the test plan that must actually have run, 0..1. ``None``
    #: disables the check - correct when no plan was built (no endpoints
    #: discovered), and the default because an existing pipeline should not
    #: start failing on a dimension it never measured before.
    min_coverage: float | None = None

    #: Errored checks are usually infrastructure, not defects: a connection
    #: refused, a timeout. Off by default for the same reason failures do not
    #: block - the triage stage is what decides whether they meant anything.
    max_errors: int | None = None

    def to_dict(self) -> dict:
        return {
            "max_critical": self.max_critical,
            "max_high": self.max_high,
            "max_medium": self.max_medium,
            "max_low": self.max_low,
            "min_coverage": self.min_coverage,
            "max_errors": self.max_errors,
        }


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    actual: float
    threshold: float
    detail: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "actual": self.actual,
            "threshold": self.threshold,
            "detail": self.detail,
        }


@dataclass
class GateDecision:
    result: str  # "pass" | "block"
    checks: list[GateCheck] = field(default_factory=list)
    severities: dict[str, int] = field(default_factory=dict)
    policy: GatePolicy = field(default_factory=GatePolicy)

    @property
    def blocked(self) -> bool:
        return self.result == "block"

    @property
    def reasons(self) -> list[str]:
        return [c.detail for c in self.checks if not c.passed]

    @property
    def exit_code(self) -> int:
        """0 to deploy, 1 to block. What CI actually consumes."""
        return 1 if self.blocked else 0

    def to_dict(self) -> dict:
        return {
            "result": self.result,
            "reasons": self.reasons,
            "severities": self.severities,
            "checks": [c.to_dict() for c in self.checks],
            "policy": self.policy.to_dict(),
        }


def count_severities(result: PipelineResult) -> dict[str, int]:
    """Count defects by severity.

    Only outcomes that became a bug report are counted. A failure classified as
    flaky or environmental has no severity because it is not a defect, which is
    the entire point of having a triage stage in front of the gate.
    """
    counts: dict[str, int] = {}
    for outcome in result.bugs:
        severity = str((outcome.bug or {}).get("severity", "unknown")).lower()
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def evaluate(result: PipelineResult, policy: GatePolicy | None = None) -> GateDecision:
    """Decide whether this result should block a deploy."""
    policy = policy or GatePolicy()
    severities = count_severities(result)
    checks: list[GateCheck] = []

    for severity, limit in (
        ("critical", policy.max_critical),
        ("high", policy.max_high),
        ("medium", policy.max_medium),
        ("low", policy.max_low),
    ):
        if limit is None:
            continue
        found = severities.get(severity, 0)
        checks.append(
            GateCheck(
                name=f"{severity}_defects",
                passed=found <= limit,
                actual=found,
                threshold=limit,
                detail=f"{found} {severity} defect(s), limit {limit}",
            )
        )

    if policy.min_coverage is not None:
        # No plan means nothing was discovered, which the discovery stage already
        # reported as an error. Failing here too would blame the gate for it.
        ratio = result.coverage.ratio if result.coverage else 1.0
        checks.append(
            GateCheck(
                name="plan_coverage",
                passed=ratio >= policy.min_coverage,
                actual=round(ratio, 3),
                threshold=policy.min_coverage,
                detail=(
                    f"{ratio:.0%} of the test plan ran, minimum {policy.min_coverage:.0%}"
                    + (
                        f" (untested: {', '.join(result.coverage.uncovered_modules)})"
                        if result.coverage and result.coverage.uncovered_modules
                        else ""
                    )
                ),
            )
        )

    if policy.max_errors is not None:
        checks.append(
            GateCheck(
                name="errored_checks",
                passed=result.errored <= policy.max_errors,
                actual=result.errored,
                threshold=policy.max_errors,
                detail=f"{result.errored} check(s) errored, limit {policy.max_errors}",
            )
        )

    blocked = any(not c.passed for c in checks)
    decision = GateDecision(
        result="block" if blocked else "pass",
        checks=checks,
        severities=severities,
        policy=policy,
    )
    logger.info(
        "quality gate: %s (%d/%d checks passed)",
        decision.result,
        sum(1 for c in checks if c.passed),
        len(checks),
    )
    return decision


def render(decision: GateDecision) -> str:
    """The CLAUDE.md section 18 block, as plain text for a CI log."""
    lines = ["QUALITY GATE", ""]
    width = max((len(c.name) for c in decision.checks), default=10)

    for check in decision.checks:
        status = "PASS" if check.passed else "FAIL"
        lines.append(f"  {check.name:<{width}}  {status:<5} {check.detail}")

    if not decision.checks:
        lines.append("  (no thresholds configured)")

    lines.append("")
    lines.append("RESULT: DEPLOY" if not decision.blocked else "RESULT: BLOCK")
    if decision.blocked:
        lines.append("")
        lines.append("Reason:")
        lines.extend(f"  {reason}" for reason in decision.reasons)
    return "\n".join(lines)
