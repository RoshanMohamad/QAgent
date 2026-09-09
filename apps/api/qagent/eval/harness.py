"""Evaluation harness (ADR-0003).

Runs the real pipeline against a fixture whose defects are known and labelled, then
scores the result. Without this there is no way to tell whether a prompt change, a
model change or a new generation rule made the product better or worse.

False-positive rate is the headline metric on purpose. A QA tool that reports
non-defects is abandoned by its users regardless of how much it recalls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from qagent.modules.llm.client import LlmClient
from qagent.pipeline import PipelineResult, run_pipeline


@dataclass
class DefectOutcome:
    defect_id: str
    endpoint: str
    title: str
    expected_class: str
    detected: bool
    observed_class: str | None = None
    detected_by: str | None = None


@dataclass
class EvaluationReport:
    fixture: str
    defects: list[DefectOutcome] = field(default_factory=list)
    false_positives: list[tuple[str, str]] = field(default_factory=list)
    reported_bugs: int = 0
    pipeline: PipelineResult | None = None

    @property
    def detection_rate(self) -> float:
        if not self.defects:
            return 0.0
        return sum(1 for d in self.defects if d.detected) / len(self.defects)

    @property
    def false_positive_rate(self) -> float:
        if not self.reported_bugs:
            return 0.0
        return len(self.false_positives) / self.reported_bugs

    @property
    def triage_accuracy(self) -> float:
        judged = [d for d in self.defects if d.observed_class]
        if not judged:
            return 0.0
        return sum(1 for d in judged if d.observed_class == d.expected_class) / len(judged)

    def to_dict(self) -> dict:
        summary = self.pipeline.summary() if self.pipeline else {}
        return {
            "fixture": self.fixture,
            "detection_rate": round(self.detection_rate, 3),
            "false_positive_rate": round(self.false_positive_rate, 3),
            "triage_accuracy": round(self.triage_accuracy, 3),
            "seeded_defects": len(self.defects),
            "detected": sum(1 for d in self.defects if d.detected),
            "reported_bugs": self.reported_bugs,
            "false_positives": [{"endpoint": e, "title": t} for e, t in self.false_positives],
            "missed": [d.defect_id for d in self.defects if not d.detected],
            "cost": summary.get("llm", {}),
            "duration_s": summary.get("duration_s"),
        }

    def render(self) -> str:
        lines = [
            f"fixture              {self.fixture}",
            f"seeded defects       {len(self.defects)}",
            f"detected             {sum(1 for d in self.defects if d.detected)}",
            "",
            f"detection rate       {self.detection_rate:.0%}",
            f"false positive rate  {self.false_positive_rate:.0%}   <- primary metric",
            f"triage accuracy      {self.triage_accuracy:.0%}",
            "",
        ]
        for defect in self.defects:
            mark = "found  " if defect.detected else "MISSED "
            lines.append(f"  {mark} {defect.defect_id}  {defect.endpoint}")
            lines.append(f"           {defect.title}")
            if defect.detected:
                lines.append(f"           classified as {defect.observed_class}")

        if self.false_positives:
            lines.append("")
            lines.append("  false positives:")
            for endpoint, title in self.false_positives:
                lines.append(f"    {endpoint}  {title}")

        if self.pipeline:
            summary = self.pipeline.summary()
            cost = summary.get("llm", {})
            lines.append("")
            lines.append(
                f"  {summary['total']} checks in {summary['duration_s']}s, "
                f"{cost.get('calls', 0)} llm calls, ${cost.get('usd', 0):.4f}"
            )

        return "\n".join(lines)


def load_ground_truth(fixtures_dir: Path, fixture_name: str) -> dict:
    path = Path(fixtures_dir) / fixture_name / "seeded_defects.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no ground truth at {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def score(result: PipelineResult, ground_truth: dict) -> EvaluationReport:
    """Match reported defects against the fixture's labels."""
    report = EvaluationReport(fixture=ground_truth.get("fixture", "unknown"), pipeline=result)

    # Reported defects, indexed by the endpoint they were found against.
    reported: dict[str, list] = {}
    for outcome in result.outcomes:
        if outcome.bug and outcome.endpoint_key:
            reported.setdefault(outcome.endpoint_key, []).append(outcome)
    report.reported_bugs = sum(len(v) for v in reported.values())

    for entry in ground_truth.get("defects", []):
        endpoint = entry["endpoint"]
        matches = reported.get(endpoint, [])
        observed = matches[0].verdict["failure_class"] if matches else None
        report.defects.append(
            DefectOutcome(
                defect_id=entry["id"],
                endpoint=endpoint,
                title=entry["title"],
                expected_class=entry.get("expected_class", "real_bug"),
                detected=bool(matches),
                observed_class=observed,
                detected_by=matches[0].name if matches else None,
            )
        )

    clean = set(ground_truth.get("clean", []))
    for endpoint, outcomes in reported.items():
        if endpoint in clean:
            for outcome in outcomes:
                report.false_positives.append((endpoint, outcome.name))

    return report


def run_evaluation(
    *,
    fixtures_dir: Path,
    fixture_name: str,
    base_url: str,
    llm: LlmClient | None = None,
) -> EvaluationReport:
    ground_truth = load_ground_truth(fixtures_dir, fixture_name)
    result = run_pipeline(
        base_url=base_url,
        llm=llm or LlmClient.from_settings(),
        allow_private=True,
    )
    return score(result, ground_truth)
